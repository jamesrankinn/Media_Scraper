#!/bin/bash
echo "Creating folders..."
mkdir -p "./Downloads/Videos"
mkdir -p "./Downloads/Images"

echo -e "\nStarting video downloads..."
# Reads video_links.txt and saves files to the Videos folder
yt-dlp --batch-file video_links.txt -P "./Downloads/Videos"

echo -e "\nStarting image/gallery downloads..."
# Reads image_links.txt and saves files to the Images folder
gallery-dl --input-file image_links.txt --directory "./Downloads/Images"

echo -e "\nAll downloads complete!"