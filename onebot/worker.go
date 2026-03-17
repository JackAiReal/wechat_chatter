package main

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"runtime/debug"
	"strings"
	"sync/atomic"
	"time"
)

func SendWorker() {
	defer func() {
		if err := recover(); err != nil {
			Error("SendWorker panic", "err", err, "stack", string(debug.Stack()))
			go SendWorker()
		}
	}()

	for m := range msgChan {
		SendWechatMsg(m)
	}
}

func drainFinishChan() {
	for {
		select {
		case <-finishChan:
			Info("丢弃过期完成信号")
		default:
			return
		}
	}
}

func SendWechatMsg(m *SendMsg) {
	time.Sleep(time.Duration(config.SendInterval) * time.Millisecond)
	currTaskId := atomic.AddInt64(&taskId, 1)
	Info("📩 收到任务", "task_id", currTaskId)

	// 避免旧任务残留的 finish 信号污染当前任务
	drainFinishChan()

	timeout := 15 * time.Second
	if m.Type == "image" || m.Type == "video" || m.Type == "send_image" || m.Type == "send_video" {
		timeout = 25 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	targetId := m.UserId
	if m.GroupID != "" {
		targetId = m.GroupID
	}

	if targetId == "" {
		Error("目标为空", "task_id", currTaskId, "target_id", targetId)
		return
	}

	waitForFinish := true

	switch m.Type {
	case "text":
		result := fridaScript.ExportsCall("triggerSendTextMessage", currTaskId, targetId, m.Content, m.AtUser)
		Info("📩 发送文本任务执行结果", "result", result, "task_id", currTaskId, "target_id", targetId, "at_user", m.AtUser)
	case "image":
		targetPath, md5Str, err := SaveBase64Image(m.Content)
		if err != nil {
			Error("保存图片失败", "err", err)
			return
		}

		result := fridaScript.ExportsCall("triggerUploadImg", targetId, md5Str, targetPath)
		Info("📩 上传图片任务执行结果", "result", result, "target_id", targetId, "md5", md5Str, "path", targetPath)

		resultStr := strings.ToLower(fmt.Sprintf("%v", result))
		if strings.Contains(resultStr, "need_init_upload_context") {
			Error("上传上下文未初始化，请先在微信中手动发一张图/视频后重试", "task_id", currTaskId, "target_id", targetId)
			waitForFinish = false
		} else if strings.Contains(resultStr, "access violation") {
			Error("上传调用异常(access violation)，本次跳过等待完成信号", "task_id", currTaskId, "target_id", targetId)
			waitForFinish = false
		}
	case "send_image":
		result := fridaScript.ExportsCall("triggerSendImgMessage", currTaskId, myWechatId, targetId)
		Info("📩 发送图片任务执行结果", "result", result, "task_id", currTaskId, "wechat_id", myWechatId, "target_id", targetId)
	case "video":
		targetPath, md5Str, err := SaveBase64Image(m.Content)
		if err != nil {
			Error("保存视频失败", "err", err)
			return
		}

		result := fridaScript.ExportsCall("triggerUploadVideo", targetId, md5Str, targetPath)
		Info("📩 上传视频任务执行结果", "result", result, "target_id", targetId, "md5", md5Str, "path", targetPath)

		resultStr := strings.ToLower(fmt.Sprintf("%v", result))
		if strings.Contains(resultStr, "need_init_upload_context") {
			Error("上传上下文未初始化，请先在微信中手动发一张图/视频后重试", "task_id", currTaskId, "target_id", targetId)
			waitForFinish = false
		} else if strings.Contains(resultStr, "access violation") {
			Error("上传调用异常(access violation)，本次跳过等待完成信号", "task_id", currTaskId, "target_id", targetId)
			waitForFinish = false
		}
	case "send_video":
		result := fridaScript.ExportsCall("triggerSendVideoMessage", currTaskId, myWechatId, targetId)
		Info("📩 发送视频任务执行结果", "result", result, "task_id", currTaskId, "wechat_id", myWechatId, "target_id", targetId)
	case "download":
		result := fridaScript.ExportsCall("triggerDownload", targetId, m.FIleCdnUrl, m.AesKey, m.FilePath, m.FileType)
		Info("📩 下载任务执行结果", "result", result, "task_id", currTaskId, "wechat_id", myWechatId, "target_id", targetId)
	}

	if !waitForFinish {
		return
	}

	select {
	case <-ctx.Done():
		Error("任务执行超时！", "taskId", currTaskId)
	case <-finishChan:
		Info("收到完成信号，任务完成", "taskId", currTaskId)
	}
}

func scheduleAutoDownloadAndNotify(meta *WechatMessage, cdnURL, aesKey, targetID, fileName string, fileType int) {
	if cdnURL == "" || aesKey == "" || targetID == "" {
		return
	}

	if _, loaded := autoDownloadInProgress.LoadOrStore(cdnURL, true); loaded {
		Info("自动下载已在进行中，跳过重复触发", "cdn_url", cdnURL)
		return
	}

	autoPath := defaultManualDownloadPath(fileType)
	msgChan <- &SendMsg{
		UserId:     targetID,
		Type:       "download",
		FIleCdnUrl: cdnURL,
		AesKey:     aesKey,
		FilePath:   autoPath,
		FileType:   fileType,
	}

	SendDownloadStatusCallback(map[string]any{
		"time":            time.Now().UnixMilli(),
		"post_type":       "notice",
		"notice_type":     "download",
		"download_status": "queued",
		"message_type":    meta.MessageType,
		"self_id":         meta.SelfID,
		"user_id":         meta.UserID,
		"group_id":        meta.GroupId,
		"message_id":      meta.MessageId,
		"file_type":       fileType,
		"file_name":       fileName,
		"cdn_url":         cdnURL,
		"file_path":       autoPath,
	})

	go func() {
		defer autoDownloadInProgress.Delete(cdnURL)
		path, err := GetDownloadPath(cdnURL, aesKey)
		if err != nil {
			SendDownloadStatusCallback(map[string]any{
				"time":            time.Now().UnixMilli(),
				"post_type":       "notice",
				"notice_type":     "download",
				"download_status": "failed",
				"message_type":    meta.MessageType,
				"self_id":         meta.SelfID,
				"user_id":         meta.UserID,
				"group_id":        meta.GroupId,
				"message_id":      meta.MessageId,
				"file_type":       fileType,
				"file_name":       fileName,
				"cdn_url":         cdnURL,
				"file_path":       "",
				"error":           err.Error(),
			})
			return
		}

		SendDownloadStatusCallback(map[string]any{
			"time":            time.Now().UnixMilli(),
			"post_type":       "notice",
			"notice_type":     "download",
			"download_status": "done",
			"message_type":    meta.MessageType,
			"self_id":         meta.SelfID,
			"user_id":         meta.UserID,
			"group_id":        meta.GroupId,
			"message_id":      meta.MessageId,
			"file_type":       fileType,
			"file_name":       fileName,
			"cdn_url":         cdnURL,
			"file_path":       "file://" + path,
		})
	}()
}

func HandleMsg(jsonData []byte) ([]byte, error) {
	m := new(WechatMessage)
	err := json.Unmarshal(jsonData, m)
	if err != nil {
		Error("解析消息失败", "err", err)
		return nil, err
	}
	myWechatId = m.SelfID
	if m.GroupId != "" {
		userID2NicknameMap.Store(m.GroupId+"_"+m.UserID, m.Sender.Nickname)
	}

	for _, msg := range m.Message {
		switch msg.Type {
		case "record":
			path, err := SaveAudioFile(msg.Data.Media)
			if err != nil {
				Error("保存音频失败", "err", err)
				return nil, err
			}
			msg.Data.URL = "file://" + path
			msg.Data.Media = nil
		case "image":
			var fileMsg FileMsg
			err = xml.Unmarshal([]byte(msg.Data.Text), &fileMsg)
			if err != nil {
				Error("XML解析失败", "err", err)
				continue
			}

			path, err := GetDownloadPath(fileMsg.Image.MidImgURL, fileMsg.Image.AesKey)
			if err != nil {
				Error("获取文件路径失败(保留原消息继续回调)", "err", err)
				continue
			}

			msg.Data.URL = "file://" + path

		case "file":
			var fileMsg FileMsg
			err = xml.Unmarshal([]byte(msg.Data.Text), &fileMsg)
			if err != nil {
				Error("XML解析失败", "err", err)
				continue
			}

			cdnURL := strings.TrimSpace(fileMsg.AppMsg.AppAttach.CdnAttachURL)
			aesKey := strings.TrimSpace(fileMsg.AppMsg.AppAttach.AesKey)
			fileName := strings.TrimSpace(fileMsg.AppMsg.Title)

			// 先保留原始消息回调，再异步下载并发送二次状态回调
			targetID := m.UserID
			if m.GroupId != "" {
				targetID = m.GroupId
			}
			scheduleAutoDownloadAndNotify(m, cdnURL, aesKey, targetID, fileName, 5)
		case "video":
			var fileMsg FileMsg
			err = xml.Unmarshal([]byte(msg.Data.Text), &fileMsg)
			if err != nil {
				Error("XML解析失败", "err", err)
				continue
			}
			path, err := GetDownloadPath(fileMsg.Video.CdnVideoUrl, fileMsg.Video.AesKey)
			if err != nil {
				Error("获取文件路径失败(保留原消息继续回调)", "err", err)
				continue
			}

			msg.Data.URL = "file://" + path
		case "face":
			var fileMsg FileMsg
			err = xml.Unmarshal([]byte(msg.Data.Text), &fileMsg)
			if err != nil {
				Error("XML解析失败", "err", err)
				return nil, err
			}

			data, err := DownloadFile(fileMsg.Emoji.ThumbUrl)
			if err != nil {
				Error("下载表情失败", "err", err)
				return nil, err
			}

			path, err := DetectAndSaveImage(data)
			if err != nil {
				Error("保存表情失败", "err", err)
				return nil, err
			}

			msg.Data.URL = "file://" + path
		}
	}
	return json.Marshal(m)
}

func GetDownloadPath(cdnUrl, aesKeyStr string) (string, error) {
	for i := 0; i < 10; i++ {
		if downloadMsgInter, ok := userID2FileMsgMap.Load(cdnUrl); ok {
			downloadReq := downloadMsgInter.(*DownloadRequest)
			if downloadReq.FilePath != "" {
				return downloadReq.FilePath, nil
			}

			// 检查数据是否还在接收中
			timeSinceLastAppend := time.Now().UnixMilli() - downloadReq.LastAppendTime
			Info("文件等待下载", "url", cdnUrl, "times", i, "last_append_time", timeSinceLastAppend)

			// 如果数据仍在接收中（1秒内有新数据），继续等待
			if timeSinceLastAppend < 1000 && i < 9 {
				time.Sleep(2 * time.Second)
				continue
			}

			// 数据接收完成，尝试解密
			if len(downloadReq.Media) > 0 {
				aesKey, err := hex.DecodeString(aesKeyStr)
				if err != nil {
					Error("AES key 解码失败", "err", err)
					return "", err
				}
				filePath, err := GetFilePath(downloadReq.Media, aesKey)
				if err != nil {
					Error("获取文件路径失败", "err", err, "media_len", len(downloadReq.Media))
					userID2FileMsgMap.Delete(cdnUrl)
					return "", err
				}

				downloadReq.FilePath = filePath
				downloadReq.Media = nil
				return filePath, nil
			}
		}

		time.Sleep(2 * time.Second)
	}

	return "", errors.New("文件下载超时或数据为空")
}
