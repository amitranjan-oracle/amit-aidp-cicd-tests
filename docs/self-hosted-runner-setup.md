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

## 2. Install the runner

Get a registration token: GitHub repo → **Settings → Actions → Runners → New
self-hosted runner** (Linux x64), or
`gh api -X POST repos/amitranjan-oracle/amit-aidp-cicd-tests/actions/runners/registration-token`.

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o runner.tar.gz -L https://github.com/actions/runner/releases/latest/download/actions-runner-linux-x64.tar.gz
tar xzf runner.tar.gz
./config.sh --url https://github.com/amitranjan-oracle/amit-aidp-cicd-tests \
            --token <REG_TOKEN> --labels self-hosted,aidp --unattended
```

## 3. Run as a service (survives reboot)

```bash
sudo ./svc.sh install opc
sudo ./svc.sh start
sudo ./svc.sh status   # expect "active (running)" and "Listening for Jobs"
```

## 4. Security (public repo)

- The workflow triggers only on `push`/`workflow_dispatch` on `main` — never
  `pull_request` from forks (which would run untrusted code on the runner).
- The runner inherits the box's instance principal; keep the IAM policy on
  `DataServices-Compute-DG` least-privilege.

## 5. First run

Push to `main` (or use **Actions → aidp-cicd → Run workflow**). The runner picks
up the job and runs `python3 aidp_cicd.py --config config/cicd.yaml`, which
ensures `/Workspace/cicd_folder`, clones/pulls the repo into the AIDP git folder,
and reconciles the `ephemeral_01` cluster + `cicd_workflow_job` job.
