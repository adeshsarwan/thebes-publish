# Running the newsroom on the VPS

The editor and publisher desk lives at **https://publish.thebesads.com**:

```
browser → Cloudflare (DNS, HTTPS, Access login) → nginx on the VPS (Cloudflare IPs only, Let's Encrypt certificate) → desk on 127.0.0.1:8765
                                                   cron, every 10 min → python -m newsroom tick  (noon pulls: New York, London, Sydney)
                                                   PostgreSQL 17 on localhost: storyhub (pipeline / newsroom / site schemas)
```

Code is in the private GitHub repo **adeshsarwan/thebes-publish**. The server pulls from it with a read-only deploy key.

| What | Where |
|---|---|
| Code, `.env` (secrets, chmod 600) | `/opt/story-hub` (user `storyhub`) |
| Pull logs, locks, `tick.log` | `/var/lib/story-hub` |
| Nightly database backups (14 days) | `/var/backups/story-hub` |
| Desk service | `systemctl status story-hub-desk` |
| Stories API service | `systemctl status story-hub-api` (logs: `journalctl -u story-hub-api`) |
| Schedule and backup jobs | `/etc/cron.d/story-hub` |
| nginx site | `/etc/nginx/sites-available/story-hub` |

Everything else on the server (other nginx sites, MySQL, RabbitMQ) is left alone.

---

## First install

### 1. Get the code onto the server (one paste, as root on 172.105.48.107)

```bash
useradd --system --create-home --home-dir /home/storyhub --shell /bin/bash storyhub
install -d -o storyhub -g storyhub /opt/story-hub
sudo -u storyhub -H bash -c 'mkdir -p ~/.ssh && ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "thebes-publish server (read-only)" && ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null'
cat /home/storyhub/.ssh/id_ed25519.pub
```

In GitHub, go to **adeshsarwan/thebes-publish → Settings → Deploy keys → Add deploy key**. Paste that line and leave "Allow write access" **off**. Then:

```bash
sudo -u storyhub -H git clone git@github.com:adeshsarwan/thebes-publish.git /opt/story-hub
```

### 2. Run the setup

```bash
cd /opt/story-hub && sudo bash deploy/setup_vps.sh
```

This installs a fresh PostgreSQL 17 (listening on this machine only) with an empty `storyhub` database. It asks for the Gemini API key, which is hidden as you type. Then it starts the desk, adds the noon schedule and nightly backups, and installs the deploy key. Running it again is safe.

At the end it prints three **GitHub Actions secrets**. Add them under **Settings → Secrets and variables → Actions → New repository secret**:

- `DEPLOY_HOST`
- `DEPLOY_KNOWN_HOSTS`
- `DEPLOY_SSH_KEY`

Then delete the printed key from the server: `rm -rf /root/story-hub-actions-key`.

From then on, every push to `main` that passes the tests is deployed automatically. The deploy key can only run `sudo /usr/local/sbin/story-hub-update`: it has no shell and can't run anything else.

### 3. First admin

```bash
sudo -u storyhub -H bash -c 'cd /opt/story-hub && .venv/bin/python -m newsroom create-admin'
```

Moving existing Mac data instead is optional. On the Mac, run `pg_dump -Fc -h localhost -d financehub -f ~/storyhub.dump` (the Mac's database keeps its old name). Copy the file to the server, then run `sudo bash deploy/setup_vps.sh --restore /path/storyhub.dump`. This only works while the server database is still empty. Either way, run `python -m newsroom schedule uninstall` on the Mac so both machines don't pull.

### 4. Certificate and Cloudflare

1. **DNS:** add an `A` record named `publish`, pointing to `172.105.48.107`, with **Proxied** (orange cloud) on.
2. **Certificate (Let's Encrypt, free):**

   ```bash
   cd /opt/story-hub && sudo bash deploy/setup_vps.sh --letsencrypt you@thebesads.com
   ```

   This gets the certificate, switches the desk to HTTPS, and sets up renewal. certbot renews it every couple of months and reloads nginx by itself. The email address only gets expiry warnings from Let's Encrypt.

   If it says Let's Encrypt could not verify the domain, the usual cause is Cloudflare's **SSL/TLS → Edge Certificates → Always Use HTTPS** redirecting the check before the server has a certificate. To get past it:
   1. Set the `publish` record to **DNS only** (grey cloud).
   2. Run the command again.
   3. Set the record back to **Proxied**.

   Renewals work while proxied, because the server answers the check over HTTPS too.
3. **SSL mode:** under **SSL/TLS → Overview**, find the zone's mode. If it is **Full (strict)**, you're done. If it is Flexible or Full, don't change it for the whole zone, because other subdomains depend on it. Instead go to **Rules → Configuration Rules → Create rule**, set **Hostname equals `publish.thebesads.com`**, and set **SSL → Full (strict)**.
4. **Access:** in **Zero Trust → Access → Applications → Add → Self-hosted**, use the domain `publish.thebesads.com`. Add a policy with action **Allow**, include **Emails ending in `@thebesads.com`** (or list specific emails), and use Google or one-time PIN as the login method. The free plan covers up to 50 users.

To use a Cloudflare origin certificate instead, save it as `/etc/ssl/story-hub/origin.pem` and its key as `origin.key`, then re-run the setup. A Let's Encrypt certificate takes priority when both exist.

### 5. Auto-publish byline

Production refuses placeholder profiles. Name a real editor or admin in `/opt/story-hub/.env`:

```
AUTOPUBLISH_RESPONSIBLE_EDITOR=adesh@thebesads.com
```

Then restart the desk with `systemctl restart story-hub-desk`.

### 6. Check it

- `https://publish.thebesads.com` should show Cloudflare Access first, then the desk login.
- `curl -I http://SERVER_IP -H 'Host: publish.thebesads.com'` from anywhere other than Cloudflare should get **403**.
- `sudo -u storyhub -H bash -c 'cd /opt/story-hub && .venv/bin/python -m newsroom schedule status'` lists the next noon slots.

---

## Stories API (for website developers)

A separate, read-only service gives developers the published stories as JSON. It runs at **https://storyhub-api.thebesads.com** (`--api-domain` changes it).

- **Service:** `story-hub-api` on 127.0.0.1:8767. It uses only the read-only `story_site` database login, so it can't change anything.
- **Keys:** admins create and revoke keys on the desk's **API** page, which also has the developer documentation. Only a hash of each key is stored. You can also manage keys from the command line: `sudo -u storyhub -H bash -c 'cd /opt/story-hub && .venv/bin/python -m storyapi keys list'`.
- **Endpoints:** `/v1/stories` (filters, paging, `updated_since` for syncing, `include_retracted`), `/v1/stories/{slug}`, `/v1/categories`, and `/v1/openapi.json`.

**One-time setup:**
1. In Cloudflare **DNS**, add an `A` record named `storyhub-api` pointing to `172.105.48.107`, with **Proxied** on.
2. Don't add it to Cloudflare Access. Developers' servers must reach it directly; the API key is the lock. If your Access application uses a wildcard like `*.thebesads.com`, exclude this hostname.
3. If the zone isn't on **Full (strict)**, add a Configuration Rule for `storyhub-api.thebesads.com` as well, just like the one for the desk.
4. On the server, run:

   ```bash
   cd /opt/story-hub && sudo bash deploy/setup_vps.sh --letsencrypt adesh@thebesads.com
   ```

   This installs the service, gets the API's certificate and sets up its site in nginx. It also refreshes the update command, so later deploys restart the API too. If Let's Encrypt can't verify the domain, use the same grey-cloud trick as for the desk.
5. Check it: `curl https://storyhub-api.thebesads.com/v1` lists the endpoints. `/v1/stories` without a key answers **401**.

## Images (stock photos, AI images, uploads)

Each story can have many images: one **main image**, images **in the story**, and spares. They come from three places:

- **Stock photos** from Unsplash and Pexels. Editors search them on the story page or in the editor. The photos are shown from the providers' own servers with a credit to the photographer, as their licences ask; we only keep a reference.
- **AI images** made with Nano Banana 2.1 at 2K, about $0.05 each. Prompts written at pick-up are saved on the story as drafts, and someone clicks **Generate** when they want them.
- **Uploads** of JPEG, PNG, WebP or GIF files.

AI images and uploads are saved as WebP in four widths to **Cloudflare R2** and served from `https://media.thebesads.com`.

Auto-published stories get images on their own. The AI picks a main image and one in-story photo from the stock results, and generates an illustration only when nothing fits. If this step fails, the story still publishes, just without images.

Images reach the website when a story is published. On a live story, an editor or publisher clicks **Update the live story's images** to send changes.

**One-time setup:**
1. **R2 bucket:** in Cloudflare, go to **R2 → Create bucket** and name it `storyhub-media`.
2. **Custom domain:** in the bucket's **Settings → Custom Domains**, connect `media.thebesads.com`. Cloudflare adds the DNS record itself. Leave the bucket's public `r2.dev` address off.
3. **R2 token:** go to **R2 → Manage R2 API Tokens → Create API token**. Choose **Object Read & Write**, limited to `storyhub-media`. Note the Access Key ID, the Secret Access Key and your Account ID.
4. **Unsplash key:** create an app at unsplash.com/developers and copy its **Access Key**. A new app works at once in demo mode (50 requests an hour). Apply for production (1,000 an hour) with screenshots of the credits on the desk.
5. **Pexels key (optional):** Pexels has paused new API keys. Without one, stock search uses Unsplash only.
6. **Server settings:** add the keys to `/opt/story-hub/.env` on the server by typing them there (`sudo nano /opt/story-hub/.env`), never in chat or email:

   ```
   R2_ACCOUNT_ID=...
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   R2_BUCKET=storyhub-media
   MEDIA_BASE_URL=https://media.thebesads.com
   UNSPLASH_ACCESS_KEY=...
   PEXELS_API_KEY=...
   # optional: IMAGE_MODEL=gemini-nano-banana-2.1
   ```

   Then run `systemctl restart story-hub-desk`.
7. **Check it:** the Images panel on any story says which parts are set up. If a key is missing, it names it.

Settings such as sizes, styles, the number of in-story images and the illustration fallback live under `images:` in `config/editorial.yaml`. Logs of background generation jobs are in `/var/lib/story-hub/logs/images-*.log`.

## Updating

Push to `main`. The **Tests and deploy** workflow runs the tests on GitHub, then connects to the server, which runs `story-hub-update`. That script:

1. pulls the code;
2. installs packages;
3. runs the tests again;
4. applies database changes and grants;
5. restarts the desk.

If any step fails, it stops before the desk is restarted. To run the same update by hand, use `sudo story-hub-update`.

## When something is wrong

| Problem | Look at |
|---|---|
| Desk not loading | `journalctl -u story-hub-desk -n 100` |
| Scheduled pulls | `/var/lib/story-hub/tick.log`, and the Sources & pulls page |
| A pull or pick-up job | `/var/lib/story-hub/logs/` |
| nginx | `nginx -t`, `/var/log/nginx/error.log` |
| 403 through Cloudflare | Cloudflare's IP list may have changed: re-run `deploy/setup_vps.sh` to refresh it |

**Restoring a backup:**

```bash
sudo -u postgres pg_restore --clean -d storyhub /var/backups/story-hub/storyhub-YYYY-MM-DD.dump
sudo -u storyhub -H bash -c 'cd /opt/story-hub && .venv/bin/python -m pipeline.cli db-setup --write-env'
```

Copy backups off the server too, using Linode Backups or a sync to object storage.
