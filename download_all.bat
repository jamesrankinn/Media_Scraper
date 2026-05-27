@echo off
echo Creating folders...
mkdir "Downloads\Videos" 2>nul
mkdir "Downloads\Images" 2>nul

echo.
echo Starting video downloads...
:: Reads video_links.txt and saves files to the Videos folder
yt-dlp --batch-file video_links.txt -P "Downloads\Videos"

echo.
echo Starting image/gallery downloads...
:: Reads image_links.txt and saves files to the Images folder
gallery-dl --input-file image_links.txt --directory "Downloads\Images"

echo.
echo All downloads complete!
pause