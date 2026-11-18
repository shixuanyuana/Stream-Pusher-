# RTSP 推流工具

根目录只保留源码和构建配置。构建产物、旧版本发布包、依赖程序和测试输出放在本地的 `archive_local/` 中，不纳入 Git。

## 源码

- `rtsp_pusher.py`
- `rtsp_pusher_gui.py`
- `rtsp_sdp_proxy.py`
- `verify_hevc_copy.py`
- `verify_release.py`
- `*.spec` / `*.iss`

## 版本管理约定

最新源码保持未提交，由维护者手动检查后提交。

旧版本发布包如果需要长期保存，建议作为 GitHub Release 附件上传；不要把 exe、ffmpeg、mediamtx 和大型 zip 直接放进 Git 提交。

## 本地归档

`archive_local/` 仅用于保留本次整理前的构建产物与旧发布包。若有文件正在运行中，Windows 可能暂时无法移动；关闭相关程序后可再移动。
