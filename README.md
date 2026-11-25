# manikin-catalog (snapshot repo)

A tiny, deliberately **separate** public repo that holds one file:
`catalog.json.gz`, the whole Supabase `swfs` table (~51k rows). It exists so the
app can read the catalog from `raw.githubusercontent.com` instead of PostgREST:
no Supabase egress, no row-count API calls, and the snapshot still works when a
free Supabase project is paused after a week of inactivity.

Everything here is standard library Python plus the daily workflow in
`.github/workflows/snapshot.yml`.

## 1. Create the repo

Web UI (no extra tooling):

1. Go to <https://github.com/new>.
2. **Repository name:** `Manikin-Catalog` (owner `clownass-op`).
3. **Public** — required: private repos need an auth token for raw downloads,
   and the exe has none.
4. Do **not** add a README, .gitignore or licence — leave it empty.
5. **Create repository**.

Or with the GitHub CLI (`winget install GitHub.cli`, then `gh auth login`):

```
gh repo create clownass-op/Manikin-Catalog --public --description "Manikin catalog snapshot (generated)"
```

## 2. Push these files

From this `catalog-snapshot` folder — it is its own repo, so init inside it:

```
cd catalog-snapshot
git init -b main
git add .
git commit -m "catalog snapshot workflow"
git remote add origin https://github.com/clownass-op/Manikin-Catalog.git
git push -u origin main
```

Keep it out of the Manikin repo: the snapshot file changes on every catalog
sync, and committing it there would grow the app repo's clone size for no gain.

## 3. Add the credentials

The publishable key is already compiled into `Wardrobe.cs`, and the table is
`SELECT`-only for `anon`, but a secret costs nothing and keeps them out of git:

1. Repo → **Settings** → **Secrets and variables** → **Actions** →
   **New repository secret**.
2. `SUPABASE_URL` = `https://zcuywyfxqvrmrmeivmcn.supabase.co`
3. `SUPABASE_KEY` = the `sb_publishable_...` key.

## 4. Run it

**Actions** → *catalog snapshot* → **Run workflow**. It takes about a minute
(52 paginated requests). Expected log:

```
  51427 rows
wrote 51427 rows: 6.9 MB raw -> 0.9 MB gzipped
```

After that it runs daily at 04:17 UTC and commits **only when the row data
changed**, so a quiet catalog means no commits at all.

To run it locally:

```
SUPABASE_URL=https://zcuywyfxqvrmrmeivmcn.supabase.co \
SUPABASE_KEY=sb_publishable_... \
python snapshot.py
```

## 5. Where the app reads it

```
https://raw.githubusercontent.com/clownass-op/Manikin-Catalog/main/catalog.json.gz
https://raw.githubusercontent.com/clownass-op/Manikin-Catalog/main/catalog.meta.json
```

`catalog.meta.json` carries `count`, uncompressed `bytes`, `sha256` and the
source table, so the app can log *why* it trusted a snapshot.

## Safety rails

- The script refuses a read that returns zero rows, and refuses an output under
  80% of the previous count — a truncated API response cannot overwrite a good
  snapshot.
- Rows are sorted by path and gzipped with `mtime=0`, so unchanged data produces
  byte-identical files and therefore no commit.
- Only the six columns the loader parses are exported (`name`, `path`,
  `category`, `item_group`, `link`, `meta`), so the file drops straight into
  `Wardrobe.AddRow` with no mapping layer.

## Scope

This mirrors the catalog **read path** only. Writes stay in Supabase
(`CatalogSync`, ~50k text rows, a few MB). The SWF blobs in `char_swfs` do not
belong here: git history keeps every deleted file forever, and GitHub blocks
files over 100 MiB — object storage (Supabase Storage or Cloudflare R2) is the
right home for those.
