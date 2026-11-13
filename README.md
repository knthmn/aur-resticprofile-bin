# aur-resticprofile-bin

Source for the AUR package [resticprofile-bin](https://aur.archlinux.org/packages/resticprofile-bin).

The AUR receives only `PKGBUILD`, `.SRCINFO` and `.gitignore`. Do not push this repository to the AUR directly: the AUR rejects subdirectories.

## Workflows

### Check for update (`update.yml`)

Runs every day and on manual dispatch. If upstream has a new release, it:

1. Bumps `PKGBUILD`, runs `updpkgsums` and regenerates `.SRCINFO` in an Arch Linux container.
2. Compares all checksums with upstream `checksums.txt`.
3. Builds and installs the x86_64 package and runs `resticprofile version`.
4. Runs the arm64, armv6 and armv7 binaries under QEMU.
5. Runs `scripts/audit.py`: release author, tag and commit signatures, late asset uploads, tarball layout, Go module hosts and pattern checks on the diff.
6. Asks Copilot for a review of the filtered diff, if the `COPILOT_PAT` secret is set.
7. Opens a pull request to `master` with the results. If the build fails, it opens an issue.

### Publish to AUR (`publish.yml`)

Runs when a push to `master` changes `PKGBUILD` or `.SRCINFO`, and on manual dispatch. It checks that `.SRCINFO` matches `PKGBUILD`, copies the package files into a clone of the AUR repository, commits with the message of the last package commit and pushes.

## Setup

- `COPILOT_PAT` secret: a fine-grained personal access token with only the "Copilot Requests" permission.
- `AUR_SSH_KEY` secret: a dedicated SSH private key. Its public key is added to the AUR account.
- Settings, Actions, General: allow GitHub Actions to create pull requests.
