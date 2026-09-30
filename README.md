# 快速知识查询

一个 Windows 常驻微应用，使用 DeepSeek V4.1 Flash（`deepseek-flash`）做低延迟知识查询和截图问答。

## 功能

- 固定知识上下文：装载知识 `A` 后，每次问题独立请求，形如 `A+B -> D`、`A+C -> E`，不会把上一轮问答继续塞进上下文。
- 文字快问：默认 `Ctrl+Alt+1`，弹出小输入框，回车发送。
- 框选截图快问：默认 `Ctrl+Alt+2`，拖动截图后直接发送给模型。
- 固定区域快问：默认 `Ctrl+Alt+3`，先设定一次截图区域，之后一键重复询问该区域。
- 流式输出：回答显示在可拖动、始终置顶的悬浮输出框。
- 快捷键录制：在应用里点“修改快捷键”，直接按新的组合键即可保存并立即生效。
- API Key 设置：应用内可填写 DeepSeek API Key。若存在 `DEEPSEEK_API_KEY` 环境变量，设置窗口会自动预填；否则留空。Key 仅保存在本次运行的内存中，不写入磁盘。

## 回答长度

系统提示词要求模型**尽量控制在 100 个汉字以内**，这是软约束，不是程序级截断。`max_output_tokens` 默认设为 `400`，避免正常回答过早被硬切断。

## 运行

### 直接使用发布版

下载 Release 中的分享包，解压后运行 `快速知识查询.exe`。

### 从源码运行

```powershell
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
python app.py
```

`config.json` 可不创建；程序有内置默认值。创建后可以修改模型、提示词、窗口尺寸、默认快捷键等。

## 构建 EXE

```powershell
.\build.ps1
```

构建结果在 `dist\快速知识查询.exe`。

## 配置与本地数据

- `config.json`：本机配置，不提交到仓库。
- `context.txt`：本机装载的知识正文，不提交到仓库。
- `state.json`：固定截图区域、悬浮窗位置等本机状态，不提交到仓库。
- API Key：仅保存在进程内存中；也可以通过 `DEEPSEEK_API_KEY` 环境变量提供。

## 网络

DeepSeek 请求明确使用直连，不继承系统代理/VPN 环境。
