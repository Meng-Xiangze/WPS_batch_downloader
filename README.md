# WPS 批量下载工具

这个工具可批量下载当前 WPS 账号有权限访问的 WPS 365 团队文件。

## 在 Windows 上运行

安装 Python 后，在项目目录运行：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

也可以双击 `start.bat` 启动。打开应用后，用 WPS 扫码登录，选择团队文件夹或输入团队文件夹链接，分析文件列表后开始下载。默认保存至项目目录下的 `downloads/`，也可在界面中选择其他目录。
