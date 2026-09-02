# Desktop releases and installers

The release process produces a native application build on each target operating
system. PyInstaller is not a cross-compiler, so Windows, macOS, and Linux artifacts
are built and tested on matching hosts.

## Supported release targets

| Target | Release | Installation |
|---|---|---|
| Windows 11+ x64 | `Setup.exe`, standalone `Uninstall.exe`, and portable `.zip` | Run Setup; Python is not required |
| macOS 14+ Intel | `.dmg` containing an app | Drag the app to Applications |
| macOS 14+ Apple silicon | `.dmg` containing an app | Drag the app to Applications |
| Ubuntu 22.04/24.04 or Debian 12/13 x64 | `.tar.gz` | Extract and run `./install.sh` |
| Ubuntu 22.04/24.04 or Debian 12/13 ARM64 | `.tar.gz` | Extract and run `./install.sh` |

Each release contains the frozen Python runtime, Excel export support, the local
interface, and the complete Manifest V3 browser extension. It deliberately does
not contain an automation browser. Weekly collection uses a current desktop Chrome
or Edge installation so it can reuse the browser profile in which the user is
already signed in. The app never imports that profile, exports cookies, or opens a
remote-debugging connection.

The extension is served as a ZIP from the running local app. Until it is published
through a browser extension store, the user loads it once with Chrome or Edge's
**Load unpacked** control. A one-time code and explicit popup approval pair that
installed extension with one local app tab. The extension then owns at most one
temporary scan tab and closes it when collection finishes.

Every artifact contains `THIRD_PARTY_NOTICES` and a generated native dependency
inventory. The build fails closed if an included native library lacks a reviewed
license mapping. Playwright and Chrome for Testing are optional developer-test
dependencies and are excluded from end-user artifacts.

## Install a published build

Verify the downloaded file against the matching line in `SHA256SUMS`, then:

- Windows: run the `Setup.exe`. The default installation is per-user and does not require administrator access. It registers the normal Windows **Installed apps** entry and adds an uninstall shortcut to the Start menu. Either of those can remove it, as can the release's standalone `Uninstall.exe`. Weekly league data under `%LOCALAPPDATA%\FantasyTradeEvaluator` is retained. The portable ZIP is a fallback for locked-down machines and can be removed by deleting its extracted folder.
- macOS: open the DMG and drag **Fantasy Trade Evaluator** to Applications. Public distribution should use a signed and notarized build; unsigned local builds can be blocked by Gatekeeper.
- Linux: extract the archive and run `./install.sh`. It verifies that the archive architecture matches the host and runs the packaged interface/extension self-check before replacing anything. It installs into the standard per-user data/bin locations, retains the preceding application bundle as `.previous`, and does not remove weekly league data when `./uninstall.sh` is run.

After the app opens, choose **Download extension** and follow the five first-time
steps shown in the interface. Chrome and Edge use the same extension files on all
supported desktop operating systems.

## Install from source or a wheel

The repository includes one guarded source bootstrap for supported Windows,
macOS, and Linux hosts:

```text
python3 bootstrap_install.py
```

Use `python bootstrap_install.py` on Windows. It requires Python 3.11 or newer,
builds a fresh user-scoped virtual environment at its final path, installs the
pinned application, and publishes the launcher only after `--self-check`
succeeds. Upgrades use a new runtime and leave the old launcher untouched on
failure. It refuses broad, external, symbolic-link, or unowned targets.
`python3 bootstrap_install.py --uninstall` removes only its marked runtime; weekly
application data is not stored there.

For a manually managed environment, install the source checkout or wheel and run
the application launcher. No browser download command is needed:

```text
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/fantasy-trade-evaluator
```

On Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\fantasy-trade-evaluator.exe
```

## Build locally

Use Python 3.12.13 on the operating system and architecture being built:

```text
python -m venv .build-venv
.build-venv/bin/python -m pip install ".[build]"
.build-venv/bin/python release_build.py
```

On Windows, use `.build-venv\Scripts\python.exe`. The Windows installer also
needs Inno Setup. The official package can be installed with
`winget install --id JRSoftware.InnoSetup -e -s winget -i`.
`--portable-only` skips that compiler and still creates the portable ZIP. Inno
Setup is free for non-commercial use; commercial builders should review and
comply with the [publisher's current license terms](https://jrsoftware.org/isorder.php).

The build downloads pinned, checksum-verified upstream license texts and
inventories the frozen native dependencies. It then runs the executable's
`--self-check`, which verifies the packaged interface, complete extension assets,
and browser-close lifecycle before an installer is created.

Developers who want to run the real extension/runtime browser smoke tests can
install the separate test extra and its isolated Chromium build:

```text
python -m pip install ".[browser-test]"
python -m playwright install --no-shell chromium
```

That browser is test infrastructure only and never enters a release artifact.

## Signing and automated builds

The workflow at `.github/workflows/release.yml` builds each OS/architecture on a
matching GitHub-hosted runner. It binds release tags exactly to `project.version`,
smoke-installs the wheel outside the checkout, and installs or mounts each finished
artifact before upload. CI installs the optional browser-test extra only to execute
the source-level extension tests.

For signed GitHub builds, configure the complete secret set
`MACOS_SIGN_IDENTITY`, `MACOS_CERTIFICATE_BASE64`,
`MACOS_CERTIFICATE_PASSWORD`, `APPLE_ID`, `APPLE_TEAM_ID`, and
`APPLE_APP_PASSWORD`. The workflow creates and unlocks a temporary keychain,
imports the Developer ID certificate and private key, stores a temporary
`notarytool` profile, and deletes the keychain after the build. PyInstaller signs
the nested binaries and app with hardened runtime; the release builder then signs
the DMG, submits it for notarization, and staples the result. With none of those
secrets, the workflow creates an explicitly unsigned development DMG. A partial
secret set fails closed.

For a local signed build, import the Developer ID identity yourself, set
`MACOS_SIGN_IDENTITY`, and set `MACOS_NOTARY_PROFILE` to an existing `notarytool`
keychain profile before running `release_build.py`.

Windows release signing is intentionally not faked. The installer is ready to
pass through an organization's existing Authenticode signing job after it is
built. A production publisher should sign `FantasyTradeEvaluator.exe`, the final
Setup executable, and the standalone Uninstall executable with its protected
certificate before distribution.

## Dependency update rule

Runtime dependencies and the build tool remain exactly pinned in `pyproject.toml`.
The `browser-test` extra is independently pinned because each Playwright version
expects a matching test browser. Changing the extension's manifest version or
operation vocabulary requires updating both the JavaScript protocol and Python
bridge contract tests. PyInstaller's [official documentation](https://pyinstaller.org/en/stable/)
confirms that it bundles Python but does not cross-compile.
