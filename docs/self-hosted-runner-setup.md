# Self-hosted runner setup — `amit-cicd-compute`

The CI/CD workflow (`.github/workflows/cicd.yml`) runs on a GitHub **self-hosted
runner** installed on `amit-cicd-compute` (private subnet, has outbound internet
via NAT). The runner dials **out** to GitHub and jobs are pushed down that
connection — no inbound port, no public IP, no SSH-from-GitHub required.

## 0. Reach the box (via the public box as bastion)

`amit-cicd-compute` is in a private subnet; reach it by jumping through the
public box `amitdemografana`:

```bash
ssh -i /path/to/ssh-key-2025-08-29.key -o IdentitiesOnly=yes \
    -J opc@144.25.95.237 opc@10.0.1.84
```

## 1. Sanity-check the instance principal + deps

```bash
python3 -c "import oci, requests, yaml; print('deps', oci.__version__)"
python3 - <<'PY'
import oci
s = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
print("instance principal OK")
PY
```

If the second snippet fails, the box isn't getting an instance principal — fix
the dynamic group (`DataServices-Compute-DG`) + the IAM policy granting it
access to `ai-data-platform-family` before continuing.

## 1b. Create an instance-principal-owned Git credential (REQUIRED for git ops)

AIDP resolves the `GIT_ACCOUNT` credential **in the caller's identity context**.
A credential created by a *user* is **invisible to the instance principal**, so
git clone/pull initiated by the box fail server-side with a generic
`InternalError`. The fix is a `GIT_ACCOUNT` setting **owned by the instance
principal**. Create it once, from the box, reading a GitHub PAT stored locally
(never echo the token):

```bash
# PAT lives on the box, e.g. /home/opc/.git_pat (chmod 600)
cd /tmp/aidp-cicd   # or wherever the repo/script is checked out on the box
python3 - <<'PY'
import sys; sys.path.insert(0, ".")
import aidp_cicd as A
c = A.AidpClient(A.load_config("config/cicd.yaml"), A.build_signer())
pat = open("/home/opc/.git_pat").read().strip()
body = {"name": "cicd-instance-principal", "isDefault": False,
        "data": {"type": "GIT_ACCOUNT", "providerName": "GITHUB",
                 "entityType": "PERSONAL_ACCESS_TOKEN",
                 "username": "amitranjan-oracle", "personalAccessToken": pat}}
r = c.request("POST", c.lake_url("userSettings"), body=body)
print("status", r.status_code, "key", r.json().get("key"))
PY
```

Put the returned key into `config/cicd.yaml` → `git.credential_key`. Verify the
instance principal now sees it: `GET {lake}/userSettings?settingType=GIT_ACCOUNT`
should list `cicd-instance-principal`. (Do **not** reuse a user-owned credential
key — clone/pull will `InternalError`.)

## 2. Get a registration token (short-lived, ~1 h)

From a machine with `gh` authenticated as a repo admin (e.g. your laptop):

```bash
# latest runner version
gh api repos/actions/runner/releases/latest --jq .tag_name        # -> v2.334.0
# registration token (single-use, expires ~1h)
gh api --method POST \
  repos/amitranjan-oracle/amit-aidp-cicd-tests/actions/runners/registration-token \
  --jq .token
```

(UI equivalent: repo → **Settings → Actions → Runners → New self-hosted runner**.)

## 3. Download, configure & register (run on the box, as `opc`)

```bash
cd ~ && mkdir -p actions-runner && cd actions-runner
curl -fsSL -o runner.tar.gz \
  https://github.com/actions/runner/releases/download/v2.334.0/actions-runner-linux-x64-2.334.0.tar.gz
tar xzf runner.tar.gz
sudo ./bin/installdependencies.sh            # OL9: pulls libicu etc.
./config.sh --url https://github.com/amitranjan-oracle/amit-aidp-cicd-tests \
            --token <REG_TOKEN> \
            --labels self-hosted,aidp --name amit-cicd-compute --unattended --replace
```

Expect `√ Runner successfully added`.

## 4. Start the runner

> ⚠️ **OL9 SELinux gotcha (as-built):** `sudo ./svc.sh install` registers a
> systemd unit, but starting it **fails** with `203/EXEC … runsvc.sh: Permission
> denied` — SELinux (enforcing) won't let **systemd** exec scripts under
> `/home/opc`. Two ways forward:

**Option A — run directly as `opc` (used now; NOT reboot-durable):**
```bash
sudo ./svc.sh stop && sudo ./svc.sh uninstall   # remove the broken unit
nohup ./run.sh > ~/actions-runner/runner.log 2>&1 &
sleep 8 && tail -5 ~/actions-runner/runner.log   # expect "Listening for Jobs"
```

**Option B — durable systemd service (recommended for production):** install the
runner **outside `/home`** so SELinux allows systemd to exec it, e.g.:
```bash
sudo mkdir -p /opt/actions-runner && sudo chown opc:opc /opt/actions-runner
# extract + ./config.sh in /opt/actions-runner instead of ~, then:
sudo ./svc.sh install opc && sudo ./svc.sh start && sudo ./svc.sh status
```
(Or keep it in `/home` and relabel: `sudo semanage fcontext -a -t bin_t '/home/opc/actions-runner/.*'` + `sudo restorecon -R ~/actions-runner` — moving to `/opt` is cleaner.)

## 5. Security (public repo)

- The workflow triggers only on `push`/`workflow_dispatch` on `main` — never
  `pull_request` from forks (which would run untrusted code on the runner).
- The runner inherits the box's instance principal; keep the IAM policy on
  `DataServices-Compute-DG` least-privilege.

## 6. Pushing the workflow file & triggering

- **Pushing `.github/workflows/cicd.yml` over HTTPS needs a token with the
  `workflow` scope.** A plain `gh` OAuth token is rejected (`refusing to allow a
  Personal Access Token to … workflow … without workflow scope`). Either
  `gh auth refresh -h github.com -s workflow`, or **push over SSH** (SSH keys are
  exempt): `git remote set-url origin git@github.com:amitranjan-oracle/amit-aidp-cicd-tests.git`.
- **`workflow_dispatch` only works once the workflow exists on the default
  branch (`main`).** Dispatching on a feature branch returns
  `HTTP 404: workflow … not found on the default branch`. So the first
  GitHub-triggered run requires the workflow merged to `main` (then `push` to
  `main` triggers it, or **Actions → aidp-cicd → Run workflow**).

## 7. First run

Once on `main`, a push (or manual dispatch) makes the runner run
`python3 aidp_cicd.py --config config/cicd.yaml`, which ensures
`/Workspace/cicd_folder`, clones/pulls the repo into the AIDP git folder, and
reconciles the `ephemeral_01` cluster + `cicd_workflow_job` job.

## As-built record (2026-06-07)

- Runner **v2.334.0** registered on `amit-cicd-compute` as `amit-cicd-compute`
  with labels `self-hosted,aidp`; running via **Option A (`nohup`)** —
  "Listening for Jobs" confirmed. Durability (Option B) is a follow-up.
- Instance-owned Git credential `cicd-instance-principal`
  (`89e86bb7-5392-4a8c-a5ec-924c87546378`) created per §1b; `config/cicd.yaml`
  references it. Clone + pull verified SUCCEEDED under the instance principal.
- Branch pushed over **SSH** (workflow-scope limitation, §6). Draft PR #3 open;
  GitHub-triggered run pending merge to `main`.
