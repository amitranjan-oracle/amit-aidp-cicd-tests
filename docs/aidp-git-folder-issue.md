# AIDP git folder: stale association after folder deletion → pull-instead-of-clone leaves a regular, partial folder

**Status:** open · **Reporter:** amitranjan · **Date:** 2026-06-08
**Component:** AIDP data-plane API handler (`datahub-dp-api`) — `GitResource` / git-folder + `gitFolderMetadata`
**Severity:** medium — breaks recovery of a deleted git folder; the folder silently becomes non-git and incomplete

---

## Summary

After a workspace **git folder** is deleted from the AIDP UI, the data-plane still
reports that path as an **associated git folder** (`gitFolderMetadata` →
`isAssociated=true` with a `repoKey`). A subsequent automated reconcile therefore
takes the **pull** path (`gitRepositories/{repoKey}/actions/pull`) instead of the
**clone** path (`POST .../gitFolders`). The pull does **not** re-clone the deleted
folder and does **not** re-establish the git-folder linkage, so the result is a
**plain (non-git) folder containing only a partial subset of the repo's files**.

Expected: deleting a git folder should clear its association so a re-create
cleanly **clones** it back as a complete git folder. Observed: the association
survives, the reconcile pulls, and the folder is left regular + incomplete.

---

## Environment (as-built)

| Item | Value |
|---|---|
| Region / host | `us-ashburn-1` / `aidp.us-ashburn-1.oci.oraclecloud.com` |
| API surface | `/20240831/dataLakes/{dataLakeId}` |
| Data lake OCID | `ocid1.datalake.oc1.iad.amaaaaaaai22xpqarb4qw6bcev7yokxk3ftd4ucefw2ofs7fbfudefs6x5sa` |
| Workspace key | `f95a83f8-9bd1-4259-a45f-ea1c3a5a7516` (playground) |
| Folder path | `/Workspace/cicd_folder/amit-aidp-cicd-tests` |
| Repo / branch | `https://github.com/amitranjan-oracle/amit-aidp-cicd-tests.git` @ `main` |
| Caller identity | VM instance principal (`DataServices-Compute-DG`) in `AI_DATA_PLATFORM_ADMIN` |

---

## Observed behavior

The reconcile (`deploy/aidp_deploy.py`, phase 2) logged:

```
== Phase 2: git folder (create or pull) ==
git folder exists; pulling main
```

i.e. `gitFolderMetadata(/Workspace/cicd_folder/amit-aidp-cicd-tests)` returned
`isAssociated=true` + a `repoKey`, even though the folder had been deleted in the
UI beforehand — so the reconcile pulled rather than cloned.

After the run, the path is a **regular folder (not a git folder)** and is
**missing files** that exist on the repo's `main`:

**Present:** `.github/workflows/{cicd-oke.yml,cicd-vm.yml}`, `.gitignore`,
`deploy/{aidp_deploy.py,cicd.yaml,test_aidp_deploy_oke.py}`,
`docs/{aidp-wi-rbac-issue.md,oke-runner-setup.md,self-hosted-runner-setup.md,superpowers/}`,
`oke/` (all 10 files).

**Missing:** `README.md`, `specs/` (`cicd_01.cluster.json`,
`cicd_workflow_job.job.json`), `src/` (`print_parameter.ipynb`,
`set_parameter.ipynb`), `deploy/requirements-cicd.txt`,
`docs/scriptable-deployment.md`.

### Re-test — deleting the folder does NOT clear the association (deterministic)

To check whether a clean delete fixes it, the **entire** `/Workspace/cicd_folder`
was deleted in the UI and the reconcile re-run (CI run `27147182125`, log):

```
Phase 1: POST .../actions/mkdir            -> HTTP 201  created /Workspace/cicd_folder   (so it WAS absent)
Phase 2: GET  .../gitFolderMetadata        -> HTTP 200  isAssociated=true, repoKey=cac15ce9-... (STALE)
         POST .../gitRepositories/cac15ce9-.../actions/pull -> HTTP 204  (pulled, not cloned)
```

So even after deleting the whole parent folder, `gitFolderMetadata` **still
reports the path as associated** and the reconcile pulls again. This reproduces
the stale-association bug deterministically and confirms that *"delete the folder
and re-run"* is **not** a workaround — the GIT_REPO record must be removed.

(That same run then failed at Phase 4 for an unrelated reason — a client-side
pagination bug in our reconcile's `list_jobs`, tracked separately, not part of
this server-side ticket.)

---

## Reproduction

1. Create a git folder in a workspace:
   `POST /workspaces/{ws}/gitFolders` with `folderPath`, `gitRepositoryUrl`,
   `branchName`, `credentialKey` → clones the repo; the folder shows as a
   **git folder** and contains the full tree.
2. **Delete that folder from the AIDP UI** (or via the objects/delete API).
3. `GET /workspaces/{ws}/gitFolderMetadata?folderPath=<same path>` →
   **observe `isAssociated=true` with a `repoKey`** (the bug: stale association
   for a folder that no longer exists).
4. Because it reports associated, call
   `POST /workspaces/{ws}/gitRepositories/{repoKey}/actions/pull`
   (the path a reconcile takes when it sees `isAssociated`).
5. **Observe:** the path is recreated as a **regular folder** (not git-linked)
   with only a **partial** file set — not a full clone.

Expected at step 3: `isAssociated=false` (no association after deletion) so that
a re-create at step 4 does a **clone** (`POST .../gitFolders`) and restores the
full tree as a proper git folder.

---

## Likely root cause (to confirm against source)

> Code-study section — see `## Code findings` below (being filled in from a read
> of the `datahub` source). Hypothesis stated here; mark inferred vs verified.

The workspace **folder object** and the **gitRepository association** appear to
have independent lifecycles. Deleting the folder removes the folder/files but
**not** the persisted `gitRepository` record, so `gitFolderMetadata` (which
derives `isAssociated`/`repoKey` from that record) keeps reporting the path as an
associated git folder — a **stale association**.

A consumer that decides clone-vs-pull on `isAssociated` then **pulls**. The pull
action assumes an existing, fully-checked-out git working tree and only applies
new commits; it does not re-clone a deleted folder and does not set the
"git folder" type/linkage on the (re)created folder object. Net: a regular folder
with whatever partial materialization the pull produced.

## Code findings

From reading `/Users/amitranjan/IdeaProjects/datahub` (module
`datahub-dp/git-service/git-service-api`). File:line references are verified;
the end-to-end *sequence* that produces the partial folder is partly inferred
(noted below).

**1. Folder deletion can leave the GIT_REPO record behind (the stale association).**
`stream/events/handlers/DeleteFileHandler.java` (≈232–265): when a deleted
folder is a git repo root, it deletes the working tree, then tries
`repoResolverRepository.deleteGitFolderPathInRepository()` (≈237) to remove the
git-repo metadata. A `404` is handled as "already gone" (OK), but **any other
exception is only logged + sent to a failure-management service, and the handler
still returns `PROCESSED` (≈262–264)** — i.e. the folder delete is reported
successful even though the GIT_REPO DB record was *not* removed. Result: a
**stale association** — folder gone, repo record remains.

**2. `gitFolderMetadata` never checks that the folder actually exists.**
`service/GitOperationsService.java` `getGitFoldersMetadatum` (≈1792–1882):
`findCandidates()` is a pure DB query over the GIT_REPO table (≈1803–1809); if a
candidate matches, it calls `worktreeTargetResolver.resolveContext()` and returns
`isAssociated=true` + `repoKey` (≈1878–1881) **without verifying the folder/`.git`
exists on the volume**. `utils/WorktreeTargetResolver.java` (≈50–139,
`resolveContext`/`findBestMatch`) does **path-segment matching only** — no
existence check. So a stale GIT_REPO record makes `isAssociated=true` for a
deleted path.

**3. PULL does not re-clone and does not mark the folder as a git folder.**
- Only the CLONE path sets the git-folder marker: `createGitFolder`
  (`GitOperationsService.java` ≈157–226) sets folder metadata
  `FOLDER_TYPE=GIT_FOLDER` (≈195). `gitPull` (≈1421–1476) creates no folder and
  sets **no** `FOLDER_TYPE` metadata — so anything it produces stays a *regular*
  folder.
- `git/exec/GitOperationExecutor.java` (≈95–102): if the repo is **not**
  initialized on FSS and the op is not CLONE, it should fail
  ("Cloned Repository does not exist").

**Inferred (not fully verifiable from code):** exactly how the PULL then leaves a
*partial regular* folder — whether git materializes some files before failing,
or a volume event replay / reconcile creates a partial tree. The agent could not
confirm the precise file-materialization path from source alone. What is solid:
(a) the stale association is left by the delete handler, (b) metadata reports it
associated without an existence check, and (c) PULL never sets the git-folder
type, so the recovered folder is regular.

---

## Requested fix

One or more of:
1. **Clean up the association on folder deletion** — deleting a git folder should
   remove (or mark detached) the `gitRepository` association so
   `gitFolderMetadata` returns `isAssociated=false` afterward.
2. **Make `gitFolderMetadata` reflect reality** — `isAssociated` should be false
   when the underlying folder object no longer exists, not just when the
   association record is absent.
3. **Make pull self-heal (or fail loudly)** — a pull against a path whose folder
   is missing/not a real git folder should either re-clone (re-establishing the
   git-folder linkage and full tree) or return a clear error, rather than
   silently producing a regular, partial folder.

---

## Impact & client-side mitigation

- **Impact:** a deleted git folder cannot be cleanly recreated by a reconcile —
  it comes back as a non-git, incomplete folder, and the broken state is silent
  (the reconcile still reports success because the pull async-op succeeds).
- **Client-side mitigation (in our reconcile):** phase 2 currently trusts
  `gitFolderMetadata.isAssociated`. We can harden it to verify the pulled folder
  is actually a complete git folder and re-clone otherwise (see
  `deploy/aidp_deploy.py`). Tracked separately from this server-side ticket.

Related: `docs/oke-runner-setup.md`, `docs/aidp-wi-rbac-issue.md`.
