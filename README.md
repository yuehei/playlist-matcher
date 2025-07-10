# Playlist Matcher for Navidrome

## 简介
本工具可帮助您将播放列表同步到Navidrome音乐服务器。支持从QQ音乐、网易云音乐和Apple Music导入歌单，并智能匹配Navidrome媒体库中的歌曲。

## 主要功能
- 支持三大音乐平台：QQ音乐、网易云音乐、Apple Music
- 智能歌曲匹配（模糊匹配/完全匹配模式）
- 自动创建并管理Navidrome歌单

## 开发要求（均在项目根目录执行）
1. Python 3.8+
2. 依赖库安装:
```bash
pip install -r requirements.txt
```
3. 需要 chrome 和 chromedriver （用于支持Apple Music），可从 https://googlechromelabs.github.io/chrome-for-testing/ 下载，下载后解压到项目根目录
4. 运行：
```bash
python playlist_matcher_navidrome.py
```
5. 打包：
```bash
pyinstaller --name "PlaylistMatcherNavidrome" --onedir --windowed --icon="icon.ico" --add-data "icon.ico;." --add-data "chrome-win64;chrome-win64" --add-data "chromedriver-win64;chromedriver-win64" playlist_matcher_navidrome.py
```

## 使用方法如图
![screenshot](./.imgs/screenshot.png)

## 说明
- `Navidrome 媒体库数据`: 在连接 Navidrome才可使用，初次使用必须“扫描 Navidrome 全库”，扫描结束可选择“导出当前数据到文件”；后续可“浏览”选择数据库文件，然后“从文件加载数据”
- `输出与服务器操作`: 可不选择结果保存位置，默认保存在软件当前目录

## 支持的播放列表来源
1. QQ音乐：支持通过URL或ID导入
2. 网易云音乐：支持通过URL或ID导入
3. Apple Music：支持通过URL或播放列表ID导入

## 贡献指南
欢迎提交Issue和PR！请遵循PEP8代码规范。

## 许可证
MIT License