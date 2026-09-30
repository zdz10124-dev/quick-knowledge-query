# 快速知识查询

Windows 快捷 AI 查询工具，使用 DeepSeek V4.1 Flash。支持文字快问、框选截图、固定区域截图，并用置顶小窗流式显示答案。

## 上下文机制

先装载固定知识 `A`，之后每次查询彼此独立：问 `B` 发送 `A+B`，再问 `C` 发送 `A+C`。上一轮问答不会进入下一轮，适合围绕同一批资料反复查询，避免聊天历史不断增长带来的额外 token。

## 截图 OCR

主界面可切换 **优先 OCR** / **不使用 OCR**。优先 OCR 时先用 Windows 自带 OCR，文字足够则只发送文本，失败或过少才回退原图；不使用 OCR 时始终发送原图。无需额外 OCR 模型。

## Token 日志

主界面会逐条记录 API 返回的：**输入 token / 缓存命中输入 token / 输出 token**。

系统提示词只要求回答尽量控制在约 100 个汉字以内，不是硬截断；`max_output_tokens` 默认 **400**，可在 `config.json` 修改。

首次使用可在应用内填写 DeepSeek API Key；若存在 `DEEPSEEK_API_KEY` 环境变量会自动读取。

默认快捷键：`Ctrl+Alt+1` 文字快问，`Ctrl+Alt+2` 框选截图，`Ctrl+Alt+3` 固定区域截图。
