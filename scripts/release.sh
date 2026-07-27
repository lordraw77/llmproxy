#!/usr/bin/env bash
#
# Helper di pubblicazione per llmproxy.
#
# Fa in un colpo solo le cose che una release richiede e che e' facile sbagliare
# nell'ordine: verifica che il repo sia in uno stato pubblicabile, crea (o
# sposta) il tag git, builda l'immagine, CONTROLLA che l'immagine costruita porti
# davvero la versione giusta, e solo allora pubblica su Docker Hub e su origin.
#
# Il punto non e' risparmiare comandi: e' che ogni passo distruttivo avvenga solo
# dopo che i controlli a monte sono passati. Tutti i controlli girano prima di
# toccare qualsiasi cosa, cosi' un problema si vede a repo intatto.
#
# Uso:
#   ./scripts/release.sh                  versione da llmproxy/__init__.py
#   ./scripts/release.sh 1.4.2            versione esplicita (deve combaciare)
#   ./scripts/release.sh -n               solo controlli + piano, non cambia nulla
#   ./scripts/release.sh -y               nessuna conferma interattiva (CI)
#
# Opzioni:
#   -n, --dry-run        esegue i controlli e stampa il piano, poi esce
#   -y, --yes            non chiede conferme (implica che sai cosa stai facendo)
#       --skip-tests     salta la suite di test (sconsigliato)
#       --multi-arch     build multi-arch via buildx (usa PLATFORMS del Makefile)
#       --no-docker      non pubblica su Docker Hub
#       --no-git         non pusha branch e tag su origin
#       --remote NAME    remote git da usare (default: origin)
#       --branch NAME    branch da cui e' lecito rilasciare (default: main)
#   -h, --help           questo aiuto

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CYA=$'\033[36m'
BLD=$'\033[1m'; DIM=$'\033[2m'; RST=$'\033[0m'

DRY_RUN=0; ASSUME_YES=0; SKIP_TESTS=0; MULTI_ARCH=0
DO_DOCKER=1; DO_GIT=1; REMOTE="origin"; VERSION_ARG=""; RELEASE_BRANCH=""

FAILURES=0

# --- output ----------------------------------------------------------------

step()  { printf '\n%s==> %s%s\n' "$BLD$CYA" "$*" "$RST"; }
ok()    { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn()  { printf '  %s!%s %s\n' "$YEL" "$RST" "$*"; }
fail()  { printf '  %s✗%s %s\n' "$RED" "$RST" "$*"; FAILURES=$((FAILURES + 1)); }
info()  { printf '    %s%s%s\n' "$DIM" "$*" "$RST"; }
die()   { printf '\n%sERRORE:%s %s\n' "$BLD$RED" "$RST" "$*" >&2; exit 1; }

usage() { sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

# Chiede conferma prima di un passo irreversibile. Con -y passa sempre.
confirm() {
    [[ $ASSUME_YES -eq 1 ]] && return 0
    local answer
    printf '\n%s%s%s [s/N] ' "$BLD" "$1" "$RST"
    read -r answer </dev/tty || answer=""
    [[ "$answer" =~ ^[sSyY]$ ]]
}

# --- argomenti --------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run)   DRY_RUN=1 ;;
        -y|--yes)       ASSUME_YES=1 ;;
        --skip-tests)   SKIP_TESTS=1 ;;
        --multi-arch)   MULTI_ARCH=1 ;;
        --no-docker)    DO_DOCKER=0 ;;
        --no-git)       DO_GIT=0 ;;
        --remote)       REMOTE="${2:?--remote richiede un nome}"; shift ;;
        --branch)       RELEASE_BRANCH="${2:?--branch richiede un nome}"; shift ;;
        -h|--help)      usage ;;
        -*)             die "opzione sconosciuta: $1 (usa --help)" ;;
        *)              VERSION_ARG="${1#v}" ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# 1. Controlli preliminari — nessuno di questi modifica qualcosa.
# ---------------------------------------------------------------------------

step "Controlli preliminari"

git rev-parse --git-dir >/dev/null 2>&1 || die "non siamo in un repository git."

# La versione sorgente e' quella del pacchetto: il tag la segue, non viceversa.
PKG_VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' llmproxy/__init__.py)"
[[ -n "$PKG_VERSION" ]] || die "non riesco a leggere __version__ da llmproxy/__init__.py."

VERSION="${VERSION_ARG:-$PKG_VERSION}"
TAG="v${VERSION}"

if [[ "$VERSION" != "$PKG_VERSION" ]]; then
    fail "versione richiesta ($VERSION) diversa da llmproxy/__init__.py ($PKG_VERSION)"
    info "allinea __version__ e committa, oppure rilascia $PKG_VERSION"
else
    ok "versione: $VERSION (da llmproxy/__init__.py)"
fi

# Il CHANGELOG e' l'unica parte della release che nessun tool puo' generare:
# se manca la sezione, quasi sicuramente e' stata dimenticata.
if grep -q "^## \[${VERSION}\]" CHANGELOG.md; then
    ok "CHANGELOG.md ha la sezione [$VERSION]"
    if ! grep -q "^\[${VERSION}\]: " CHANGELOG.md; then
        warn "manca il link di confronto in fondo al CHANGELOG:"
        info "[$VERSION]: https://github.com/lordraw77/llmproxy/compare/v<prec>...v${VERSION}"
    fi
    if grep -q "^## \[Unreleased\]\s*$" CHANGELOG.md && \
       [[ "$(grep -A2 '^## \[Unreleased\]' CHANGELOG.md | tail -1)" != "" ]]; then
        : # sezione Unreleased non vuota: informativo, non bloccante
    fi
else
    fail "CHANGELOG.md non ha una sezione '## [$VERSION]'"
fi

# Working tree pulito. E' esattamente la condizione che fa comparire "-dirty"
# nella versione: git describe guarda i file tracciati, quindi anche noi.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    fail "ci sono modifiche non committate (la versione diventerebbe ${VERSION}-dirty)"
    git status --short --untracked-files=no | sed "s/^/      ${DIM}/;s/$/${RST}/"
else
    ok "working tree pulito"
fi

UNTRACKED="$(git ls-files --others --exclude-standard | head -5)"
if [[ -n "$UNTRACKED" ]]; then
    warn "file non tracciati (non bloccano la release, non entrano nell'immagine):"
    printf '      %s%s%s\n' "$DIM" "$(echo "$UNTRACKED" | tr '\n' ' ')" "$RST"
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# Si rilascia dal branch di default (quello che origin/HEAD indica, di norma
# main): il tag deve stare sulla storia che tutti vedono, non su un branch di
# lavoro che magari non verra' mai mergiato. Con --branch si sceglie altro.
if [[ -z "$RELEASE_BRANCH" ]]; then
    RELEASE_BRANCH="$(git symbolic-ref -q --short "refs/remotes/${REMOTE}/HEAD" 2>/dev/null || true)"
    RELEASE_BRANCH="${RELEASE_BRANCH#${REMOTE}/}"
    RELEASE_BRANCH="${RELEASE_BRANCH:-main}"
fi

if [[ "$BRANCH" == "$RELEASE_BRANCH" ]]; then
    ok "branch corrente: $BRANCH"
elif [[ "$BRANCH" == "HEAD" ]]; then
    fail "HEAD e' staccato (detached): fai checkout di $RELEASE_BRANCH prima di rilasciare"
else
    fail "sei su '$BRANCH', ma le release si fanno da '$RELEASE_BRANCH'"
    info "mergia su $RELEASE_BRANCH e riprova, oppure usa --branch $BRANCH se e' voluto"
fi

# Stato del tag: e' il punto in cui si e' gia' sbagliato una volta.
TAG_ACTION="create"
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
    TAGGED_COMMIT="$(git rev-list -n1 "$TAG")"
    if [[ "$TAGGED_COMMIT" == "$(git rev-parse HEAD)" ]]; then
        TAG_ACTION="reuse"
        ok "il tag $TAG esiste gia' e punta a HEAD"
    else
        TAG_ACTION="move"
        warn "il tag $TAG esiste ma punta a un altro commit:"
        info "$(git log -1 --oneline "$TAGGED_COMMIT")"
        info "HEAD e': $(git log -1 --oneline HEAD)"
        if git ls-remote --tags "$REMOTE" 2>/dev/null | grep -q "refs/tags/${TAG}$"; then
            fail "il tag $TAG e' GIA' PUBBLICATO su $REMOTE: spostarlo riscrive storia condivisa"
            info "usa una nuova versione (es. ${VERSION%.*}.$(( ${VERSION##*.} + 1 ))) invece di spostarlo"
        else
            info "non e' su $REMOTE: si puo' spostare senza conseguenze per altri"
        fi
    fi
else
    ok "il tag $TAG non esiste ancora"
fi

# Docker serve solo se davvero pubblichiamo l'immagine.
if [[ $DO_DOCKER -eq 1 ]]; then
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        ok "docker disponibile"
    else
        fail "docker non disponibile o daemon non in esecuzione"
    fi
fi

# --- test -------------------------------------------------------------------

if [[ $SKIP_TESTS -eq 1 ]]; then
    warn "test saltati (--skip-tests)"
else
    step "Suite di test"
    # Nessun ARGS: pytest.ini imposta gia' -q, e un secondo -q diventa -qq, che
    # sopprime anche la riga di riepilogo da cui leggiamo il conteggio.
    if make test >/tmp/llmproxy-release-tests.log 2>&1; then
        ok "$(grep -Eo '[0-9]+ passed.*' /tmp/llmproxy-release-tests.log | tail -1 || echo 'tutti i test passano')"
    else
        fail "la suite di test fallisce"
        tail -20 /tmp/llmproxy-release-tests.log | sed "s/^/      ${DIM}/;s/$/${RST}/"
    fi
fi

# ---------------------------------------------------------------------------
# 2. Piano
# ---------------------------------------------------------------------------

IMAGE="$(make -s version 2>/dev/null | sed -n 's/^IMAGE   = //p')"
IMAGE="${IMAGE:-lordraw/llmproxy}"

step "Piano di pubblicazione"
printf '  versione   %s%s%s\n' "$BLD" "$VERSION" "$RST"
printf '  tag git    %s (%s)\n' "$TAG" \
    "$([[ $TAG_ACTION == create ]] && echo 'da creare' || \
       ([[ $TAG_ACTION == reuse ]] && echo 'gia su HEAD' || echo 'da SPOSTARE su HEAD'))"
printf '  immagine   %s:%s%s\n' "$IMAGE" "$VERSION" \
    "$([[ $MULTI_ARCH -eq 1 ]] && echo ' (multi-arch)' || echo '')"
printf '             %s:latest\n' "$IMAGE"
printf '  docker hub %s\n' "$([[ $DO_DOCKER -eq 1 ]] && echo 'push' || echo 'no (--no-docker)')"
printf '  git push   %s\n' "$([[ $DO_GIT -eq 1 ]] && echo "$REMOTE $BRANCH + $TAG" || echo 'no (--no-git)')"

if [[ $FAILURES -gt 0 ]]; then
    die "$FAILURES controllo/i fallito/i: risolvi prima di pubblicare."
fi

if [[ $DRY_RUN -eq 1 ]]; then
    printf '\n%s--dry-run: nessuna modifica effettuata.%s\n' "$GRN" "$RST"
    exit 0
fi

confirm "Procedere?" || { echo "Annullato."; exit 0; }

# ---------------------------------------------------------------------------
# 3. Esecuzione — da qui in poi si modifica lo stato.
# ---------------------------------------------------------------------------

step "Tag git"
case "$TAG_ACTION" in
    reuse) ok "$TAG gia' su HEAD, lascio com'e'" ;;
    create)
        git tag -a "$TAG" -m "$VERSION"
        ok "creato $TAG su $(git rev-parse --short HEAD)" ;;
    move)
        confirm "Spostare il tag $TAG su HEAD?" || die "annullato: il tag non e' su HEAD."
        git tag -f -a "$TAG" -m "$VERSION" HEAD >/dev/null
        ok "spostato $TAG su $(git rev-parse --short HEAD)" ;;
esac

# Ultima verifica prima di buildare: la versione che il Makefile calcolera'
# davvero. Se qui non e' pulita, l'immagine nascerebbe gia' etichettata male.
COMPUTED="$(git describe --tags --always --dirty | sed 's/^v//')"
[[ "$COMPUTED" == "$VERSION" ]] || \
    die "il Makefile calcolerebbe '$COMPUTED' invece di '$VERSION'. Non procedo."
ok "versione calcolata dal Makefile: $COMPUTED"

step "Build immagine"
if [[ $MULTI_ARCH -eq 1 ]]; then
    # buildx pubblica direttamente: build e push sono lo stesso passo.
    [[ $DO_DOCKER -eq 1 ]] || die "--multi-arch pubblica in fase di build: incompatibile con --no-docker."
    confirm "buildx builda e PUBBLICA in un solo passo. Confermi?" || { echo "Annullato."; exit 0; }
    make buildx-release
    ok "immagine multi-arch pubblicata"
else
    make build
    ok "immagine costruita"

    # Il controllo che sarebbe servito la volta scorsa: la versione finisce
    # come label OCI dentro l'immagine, dove un `docker tag` non la corregge.
    LABEL="$(docker inspect "${IMAGE}:${VERSION}" \
             --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || true)"
    if [[ "$LABEL" == "$VERSION" ]]; then
        ok "label OCI dell'immagine: $LABEL"
    else
        die "l'immagine porta la label version='$LABEL' invece di '$VERSION'. Non la pubblico."
    fi

    if [[ $DO_DOCKER -eq 1 ]]; then
        step "Push su Docker Hub"
        confirm "Pubblicare ${IMAGE}:${VERSION} e ${IMAGE}:latest su Docker Hub?" \
            || { echo "Annullato (il tag git resta creato)."; exit 0; }
        make push
        ok "pubblicata ${IMAGE}:${VERSION} e ${IMAGE}:latest"
    else
        warn "push Docker Hub saltato (--no-docker)"
    fi
fi

if [[ $DO_GIT -eq 1 ]]; then
    step "Push su $REMOTE"
    confirm "Pushare $BRANCH e il tag $TAG su $REMOTE?" \
        || { echo "Annullato (immagine gia' pubblicata)."; exit 0; }
    git push "$REMOTE" "$BRANCH"
    # --force serve solo quando il tag e' stato spostato; e' innocuo altrimenti,
    # ma lo usiamo solo in quel caso per non nascondere errori.
    if [[ "$TAG_ACTION" == "move" ]]; then
        git push --force "$REMOTE" "$TAG"
    else
        git push "$REMOTE" "$TAG"
    fi
    ok "pushati $BRANCH e $TAG"
else
    warn "push git saltato (--no-git)"
fi

step "Release $VERSION completata"
[[ $DO_DOCKER -eq 1 ]] && info "docker pull ${IMAGE}:${VERSION}"
[[ $DO_GIT -eq 1 ]] && info "https://github.com/lordraw77/llmproxy/releases/tag/${TAG}"
echo
