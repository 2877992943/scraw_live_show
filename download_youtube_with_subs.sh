#!/bin/bash

# 下载 YouTube 视频，并嵌入简体中文 + 英文字幕
# 用法：./download_youtube_with_subs.sh "https://www.youtube.com/watch?v=VIDEO_ID"
 # 脚本流程：
 #  1. 用 yt-dlp 下载视频，读取 Chrome Cookie 绕过验证；
 #  2. 下载并嵌入 简体中文（zh-Hans）和 英文（en）字幕；
 #  3. 先用 MKV 封装确保字幕能成功嵌入；
 #  4. 再用 ffmpeg 转成兼容性更好的 MP4（H.264 + AAC）；
 #  5. 自动删除中间 MKV 文件，最终只保留 .mp4。

 #  需要环境：yt-dlp、ffmpeg、node 和 Chrome 浏览器已登录 YouTube。


set -e

URL="$1"

if [ -z "$URL" ]; then
    echo "用法：$0 <YouTube 视频链接>"
    echo "示例：$0 \"https://www.youtube.com/watch?v=uD4-uy0GmHE\""
    exit 1
fi

# 1. 用 yt-dlp 下载视频 + 字幕，并封装为 MKV
#    --cookies-from-browser chrome  读取 Chrome 的 YouTube Cookie，避免 bot 验证
#    --write-auto-subs              下载自动生成的字幕（YouTube 没有手工中文时必需）
#    --sub-langs zh-Hans,en         指定简体中文和英文
#    --embed-subs                   把字幕嵌入视频文件
yt-dlp \
    --js-runtimes node \
    --remote-components ejs:github \
    --cookies-from-browser chrome \
    --embed-subs \
    --write-auto-subs \
    --sub-langs zh-Hans,en \
    --convert-subs srt \
    --write-subs \
    --remux-video mkv \
    -o "%(title)s.%(ext)s" \
    "$URL"

# 2. 找到刚下载的 MKV 文件
MKV_FILE=$(ls -t *.mkv | head -n 1)

if [ -z "$MKV_FILE" ]; then
    echo "未找到下载的 MKV 文件"
    exit 1
fi

# 3. 转换为兼容性更好的 MP4（H.264 + AAC），并保留字幕
MP4_FILE="${MKV_FILE%.mkv}.mp4"

ffmpeg -i "$MKV_FILE" \
    -map 0 \
    -c:v libx264 -crf 23 -preset medium \
    -c:a aac -b:a 128k \
    -c:s mov_text \
    "$MP4_FILE"

# 4. 删除中间 MKV 文件（如需保留可注释掉下一行）
rm "$MKV_FILE"

echo "完成：$MP4_FILE"
