# 安装部署

`setup/` 目录提供**一键安装部署**程序，用于在新机器上自动检查并补齐项目缺失的依赖与文件。

## 使用方式

双击 `setup\install.bat`，或在命令行运行：

```bash
python setup\install.py
```

安装程序会自动完成：

1. **检查项目文件完整性**（`app.py`、`core/`、`web/`、`plugins/` 等）。
2. **创建虚拟环境**（`venv/`，已存在则跳过）并**安装全部 Python 依赖**（`requirements.txt`）。
3. **创建运行时目录**（`runtime/tts`、`runtime/music`）。
4. **可选安装 Whisper 语音识别模型**（约 500MB，需联网）：
   - 选择安装 → 自动下载 faster-whisper-small 到 `models/whisper`；
   - 选择跳过 → 提示**语音识别功能不可用**（文字对话不受影响）。
5. **检查 GPT-SoVITS 语音合成**（外部整合包，无法自动下载）：
   - 仅检查 `launcher_config.json` 中配置的 `api_v2.py` 路径；
   - 未配置/跳过 → 提示**语音合成功能不可用**。
6. **检查 ffmpeg**（点歌转码需要；缺失仅影响点歌）。

## 说明

- 本安装程序**只新建/安装缺失内容**（venv、依赖、模型、runtime 目录），
  **不会修改项目已有的代码与配置文件**。
- 安装完成后：编辑 `launcher_config.json` 确认 GPT-SoVITS 路径 → 双击根目录 `start.bat` 启动。
- 未选装语音识别 / 语音合成时，对应语音功能不可用，但文字对话、联网搜索、点歌（需 ffmpeg）等其余功能正常。
