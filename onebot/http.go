package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime/debug"
	"strings"
	"time"
)

func sendHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "仅支持 POST", http.StatusMethodNotAllowed)
		Error("仅支持 POST")
		return
	}

	req := new(SendRequest)
	if err := json.NewDecoder(r.Body).Decode(req); err != nil {
		http.Error(w, "无效的 JSON", http.StatusBadRequest)
		Error("无效的 JSON")
		return
	}

	// 参数校验
	if len(req.Message) == 0 || (req.UserID == "" && req.GroupID == "") {
		http.Error(w, "参数缺失", http.StatusBadRequest)
		Error("参数缺失")
		return
	}

	sendContent := ""
	atUserID := ""
	for _, v := range req.Message {
		if v.Type == "text" {
			sendContent += v.Data.Text
		} else if v.Type == "at" {
			if req.GroupID != "" {
				if nicknameInter, ok := userID2NicknameMap.Load(req.GroupID + "_" + v.Data.QQ); ok {
					sendContent += fmt.Sprintf("@%s\u2005", nicknameInter.(string))
					atUserID += v.Data.QQ + ","
				}
			}

		} else if v.Type == "image" || v.Type == "video" {
			msgChan <- &SendMsg{
				UserId:  req.UserID,
				GroupID: req.GroupID,
				Content: v.Data.File,
				Type:    v.Type,
			}
		} else {
			Info("暂不支持的发送消息类型，已忽略", "type", v.Type)
		}
	}

	if sendContent != "" {
		msgChan <- &SendMsg{
			UserId:  req.UserID,
			GroupID: req.GroupID,
			Content: sendContent,
			Type:    "text",
			AtUser:  strings.TrimRight(atUserID, ","),
		}
	}

	json.NewEncoder(w).Encode(map[string]any{
		"status": "ok",
	})
}

func defaultManualDownloadPath(fileType int) string {
	ext := "bin"
	switch fileType {
	case 1, 2, 3:
		ext = "jpg"
	case 4:
		ext = "mp4"
	case 5:
		ext = "dat"
	}
	baseDir := "."
	if exePath, err := os.Executable(); err == nil {
		baseDir = filepath.Dir(exePath)
	}
	targetDir := filepath.Join(baseDir, "file", "manual_download")
	_ = os.MkdirAll(targetDir, 0o755)
	return filepath.Join(targetDir, fmt.Sprintf("%d_manual.%s", time.Now().UnixNano(), ext))
}

func downloadHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "仅支持 POST", http.StatusMethodNotAllowed)
		Error("仅支持 POST")
		return
	}

	req := new(ManualDownloadRequest)
	if err := json.NewDecoder(r.Body).Decode(req); err != nil {
		http.Error(w, "无效的 JSON", http.StatusBadRequest)
		Error("无效的 JSON", "err", err)
		return
	}

	if req.TargetID == "" || req.CdnURL == "" || req.AesKey == "" || req.FileType == 0 {
		http.Error(w, "参数缺失: target_id/cdn_url/aes_key/file_type 必填", http.StatusBadRequest)
		Error("下载参数缺失", "target_id", req.TargetID, "cdn_url", req.CdnURL, "file_type", req.FileType)
		return
	}

	if req.FilePath == "" {
		req.FilePath = defaultManualDownloadPath(req.FileType)
	}

	msgChan <- &SendMsg{
		UserId:     req.TargetID,
		Type:       "download",
		FIleCdnUrl: req.CdnURL,
		AesKey:     req.AesKey,
		FilePath:   req.FilePath,
		FileType:   req.FileType,
		Md5:        req.MD5,
	}

	Info("已加入手动下载任务队列", "target_id", req.TargetID, "file_type", req.FileType, "file_path", req.FilePath)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"status":    "ok",
		"queued":    true,
		"file_path": req.FilePath,
	})
}

func sendCallbackPayload(jsonReq []byte) {
	Info("发送数据", "msg", string(jsonReq))
	req, err := http.NewRequest("POST", config.SendURL, bytes.NewBuffer(jsonReq))
	if err != nil {
		Error("创建请求失败", "err", err)
		return
	}

	// 统一签名头
	h := hmac.New(sha1.New, []byte(config.OnebotToken))
	h.Write(jsonReq)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Signature", "sha1="+hex.EncodeToString(h.Sum(nil)))

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		Error("请求执行失败", "err", err)
		return
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		Error("读取响应失败", "err", err)
		return
	}

	Info("返回内容", "status", resp.StatusCode, "body", string(body))
}

// SendDownloadStatusCallback 用于二次回调自动下载状态
func SendDownloadStatusCallback(event map[string]any) {
	payload, err := json.Marshal(event)
	if err != nil {
		Error("下载状态回调序列化失败", "err", err)
		return
	}
	sendCallbackPayload(payload)
}

func SendHttpReq(jsonData []byte) {
	defer func() {
		if r := recover(); r != nil {
			Error("http panic", "err", r, "stack", string(debug.Stack()))
		}
	}()

	time.Sleep(time.Duration(config.SendInterval) * time.Millisecond)
	jsonReq, err := HandleMsg(jsonData)
	if err != nil {
		Error("JSON 序列化失败", "err", err)
		return
	}
	if jsonReq == nil {
		return
	}

	sendCallbackPayload(jsonReq)
}
