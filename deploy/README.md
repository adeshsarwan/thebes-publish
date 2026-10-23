# Running the newsroom on the VPS

The editor and publisher desk lives at **https://publish.thebesads.com**:

```
browser → Cloudflare (DNS, HTTPS, Access login) → nginx on the VPS (Cloudflare IPs only) → desk on 127.0.0.1:8765
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
| Schedule and backup jobs | `/etc/cron.d/story-hub` |
| nginx site | `/etc/nginx/sites-available/story-hub` |

Everything else on the server (other nginx sites, MySQL, RabbitMQ) is left alone.

---

## First install

### 1. Get the code onto the server (one paste, as root on 172.236.151.195)

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

### 4. Cloudflare

1. **Origin certificate:** go to **SSL/TLS → Origin Server → Create certificate** for `publish.thebesads.com`, keeping the PEM format. On the server, save the certificate as `/etc/ssl/story-hub/origin.pem` and the private key as `/etc/ssl/story-hub/origin.key`. Then run `sudo bash deploy/setup_vps.sh` again, which turns on the nginx site.
2. **SSL mode:** under **SSL/TLS → Overview**, find the zone's mode. If it is **Full (strict)**, you're done. If it is Flexible or Full, don't change it for the whole zone, because other subdomains depend on it. Instead go to **Rules → Configuration Rules → Create rule**, set **Hostname equals `publish.thebesads.com`**, and set **SSL → Full (strict)**.
3. **DNS:** add an `A` record named `publish`, pointing to the server's IP, with **Proxied** (orange cloud) on.
4. **Access:** in **Zero Trust → Access → Applications → Add → Self-hosted**, use the domain `publish.thebesads.com`. Add a policy with action **Allow**, include **Emails ending in `@thebesads.com`** (or list specific emails), and use Google or one-time PIN as the login method. The free plan covers up to 50 users.

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
