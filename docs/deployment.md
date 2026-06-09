# Docs & Website Deployment

Two independent publishing surfaces:

| Surface | Source | URL pattern |
|---------|--------|-------------|
| **GitHub Pages** (website) | `docs/index.html` on `master` | `https://<user>.github.io/<repo>/` |
| **GitHub Wiki** (markdown docs) | `docs/*.md` pushed to wiki | `https://github.com/<user>/<repo>/wiki` |

---

## GitHub Pages (the visual site)

The site lives entirely in `docs/index.html` — no build step, no Jekyll.

### Enable on GitHub

1. Repo → **Settings** → **Pages** (left sidebar)
2. **Build and deployment**:
   - Source: `Deploy from a branch`
   - Branch: `master`
   - Folder: `/docs`
3. **Save**

Pages goes live within ~60 s at `https://<user>.github.io/<repo>/`.
Every push to `master` that touches `docs/` triggers an automatic redeploy.

### Add screenshots

Drop PNG files into `docs/screenshots/` — the gallery loads them automatically.
Expected filenames (placeholders shown until files exist):

```
docs/screenshots/dashboard-overview.png
docs/screenshots/dashboard-analytics.png
docs/screenshots/dashboard-billing.png
docs/screenshots/dashboard-monitoring.png
docs/screenshots/openwebui-chat.png
docs/screenshots/openwebui-models.png
```

### Update site content

Edit `docs/index.html` directly — all CSS and JS are inline.
Content sections map to anchors: `#overview`, `#architecture`, `#install`, `#usage`, `#providers`, `#cli`, `#screenshots`.

---

## Alternative Hosting

### Netlify (drag-and-drop or git)

**Git-connected (auto-deploy on push):**
1. netlify.com → **Add new site** → **Import from Git**
2. Connect repo, set:
   - Base directory: `docs`
   - Publish directory: `docs`
   - Build command: *(leave blank)*
3. Deploy. Custom domain available in site settings.

**Manual drag-and-drop:**
```bash
# Zip the docs/ folder and upload at app.netlify.com/drop
zip -r site.zip docs/
```

### Vercel

```bash
npm i -g vercel
cd docs
vercel --name self-host-llm
# Vercel detects static HTML, deploys instantly
```

Or connect via vercel.com dashboard → Import Git Repository → set Root Directory to `docs`.

### Self-hosted nginx

```nginx
server {
    listen 80;
    server_name your-domain.com;
    root /var/www/self-host-llm/docs;
    index index.html;
    location / { try_files $uri $uri/ =404; }
}
```

```bash
# Deploy
rsync -av docs/ user@host:/var/www/self-host-llm/docs/
```

### GitHub Codespaces / local preview

```bash
cd docs
python3 -m http.server 8080
# open http://localhost:8080
```

---

## GitHub Wiki

GitHub Wiki is a separate git repository attached to the main repo.
It renders the `docs/*.md` files with standard GitHub Markdown.

### Enable

Repo → **Settings** → **Features** → check **Wikis** → Save.

### Push docs to wiki

```bash
# Clone the wiki repo (separate from main repo)
git clone https://github.com/<user>/<repo>.wiki.git wiki-repo
cd wiki-repo

# Copy markdown docs
cp ../docs/tiers.md     Tiers.md
cp ../docs/providers.md Providers.md
cp ../docs/cli.md       CLI-Reference.md
cp ../docs/development.md Development.md
cp ../docs/embeddings.md  Embeddings.md
cp ../docs/pipeline.md    Pipeline.md

# Home page (wiki landing)
cp ../README.md Home.md

git add .
git commit -m "sync docs from main repo"
git push
```

Wiki is now browsable at `https://github.com/<user>/<repo>/wiki`.

### Keep wiki in sync

Add a script `scripts/sync-wiki.sh`:

```bash
#!/usr/bin/env bash
set -e
WIKI_DIR=$(mktemp -d)
git clone "https://github.com/${GITHUB_REPO}.wiki.git" "$WIKI_DIR"

cp docs/tiers.md      "$WIKI_DIR/Tiers.md"
cp docs/providers.md  "$WIKI_DIR/Providers.md"
cp docs/cli.md        "$WIKI_DIR/CLI-Reference.md"
cp docs/development.md "$WIKI_DIR/Development.md"
cp docs/embeddings.md  "$WIKI_DIR/Embeddings.md"
cp docs/pipeline.md   "$WIKI_DIR/Pipeline.md"
cp README.md          "$WIKI_DIR/Home.md"

cd "$WIKI_DIR"
git add .
git diff --cached --quiet && echo "Wiki already up to date." && exit 0
git commit -m "sync docs $(date +%Y-%m-%d)"
git push
rm -rf "$WIKI_DIR"
```

Or automate with a GitHub Actions workflow:

```yaml
# .github/workflows/sync-wiki.yml
name: Sync Wiki
on:
  push:
    branches: [master]
    paths: ['docs/**', 'README.md']
jobs:
  wiki:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Push to wiki
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          git config --global user.email "actions@github.com"
          git config --global user.name  "GitHub Actions"
          bash scripts/sync-wiki.sh
```

> **Note:** `GITHUB_TOKEN` has wiki write access by default on public repos.
> On private repos, generate a PAT with `repo` scope and store as a repository secret.
