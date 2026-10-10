# YouTube Clip Studio (GitHub Actions)

Create Shorts-sized video cuts and Persian/Bengali/Arabic/English YouTube publication copy from a long-form source using Gemini video understanding and FFmpeg. Outputs are downloadable GitHub Actions artifacts, not auto-uploaded YouTube videos.

**Start with [README_FA.md](README_FA.md) for detailed installation and use in Persian.**

1. Create a **private** GitHub repository and commit this project, including `.github/workflows/youtube-studio.yml`.
2. Set repository Actions secret `GEMINI_API_KEY`.
3. From **Actions → YouTube Clip Studio → Run workflow**, enter a downloadable file URL, Drive share link, or a permissible public YouTube URL.
4. Download the `youtube-studio-*` artifact and open `dashboard.html`.

Caveats: metadata and cut points are AI drafts and must be reviewed; no SRT or YouTube upload in this release. Actions quota/storage and Gemini API cost/free-tier limitations apply. See the Persian README.
