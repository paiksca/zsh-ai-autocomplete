#!/usr/bin/env zsh
# =====================================================================
# ai-zsh — Gemini-powered, context-aware ghost-text autocomplete for zsh
#          + `prompt <natural language>` -> command (with approval),
#          leveraging zoxide & fzf.
#
# Source this AFTER zsh-autosuggestions and BEFORE zsh-syntax-highlighting.
# =====================================================================

# --- config (override any of these before sourcing, or via env) -------
: ${AIZSH_DIR:="${0:A:h}"}
: ${AIZSH_BACKEND:="$AIZSH_DIR/backend.py"}
: ${AIZSH_PYTHON:=python3}
: ${AIZSH_SOCK:="${TMPDIR:-/tmp}/ai-zsh-${UID}.sock"}
: ${AIZSH_LOG:="${TMPDIR:-/tmp}/ai-zsh.log"}
: ${AIZSH_DEBOUNCE:=0.18}     # seconds of idle before an async AI fetch fires
: ${AIZSH_MIN_LEN:=2}         # min buffer length before trying AI ghost text
: ${AIZSH_HIST_N:=15}         # recent history lines sent as context
: ${AIZSH_FORCE_KEY:=^o}      # key to force an AI ghost suggestion on demand
: ${AIZSH_PREDICT:=1}         # predict the next command on an empty prompt
: ${AIZSH_STATS:=1}           # instant statistical ghost (frecency/dir/sequence) under the LLM
: ${AIZSH_WORD_ACCEPT_KEY:=^[[1;5C}   # Ctrl-Right: accept the next word of the ghost
export AIZSH_SOCK             # backend (daemon + oneshot) must use the same path

# AI ghost text needs async, otherwise every keystroke would block on the network.
ZSH_AUTOSUGGEST_USE_ASYNC=1
# Order: AI (smart) → stats (instant local) → history. First non-empty wins; the
# instant stats placeholder is rendered synchronously and the LLM refines it.
typeset -ga ZSH_AUTOSUGGEST_STRATEGY
if [[ ${#ZSH_AUTOSUGGEST_STRATEGY} -eq 0 \
      || "${ZSH_AUTOSUGGEST_STRATEGY[*]}" == "history" \
      || "${ZSH_AUTOSUGGEST_STRATEGY[*]}" == "ai history" ]]; then
    ZSH_AUTOSUGGEST_STRATEGY=(ai stats history)
fi

zmodload zsh/net/socket 2>/dev/null   # for the pure-zsh daemon client

# --- small helpers ----------------------------------------------------
# AI is available if a local provider is set, or a Gemini key is present.
_aizsh_have_ai() { [[ "$AIZSH_PROVIDER" == (ollama|openai) || -n "$GEMINI_API_KEY" ]]; }
_aizsh_b64()  { print -rn -- "$1" | base64 | tr -d '\n'; }
_aizsh_b64d() { print -rn -- "$1" | base64 -d 2>/dev/null; }

# Read a string field from a JSON object on stdin.
_aizsh_json_get() {
    if (( $+commands[jq] )); then
        jq -r --arg k "$1" '.[$k] // empty' 2>/dev/null
    else
        "${=AIZSH_PYTHON}" -c 'import sys,json
try: print(json.load(sys.stdin).get(sys.argv[1],"") or "")
except Exception: pass' "$1" 2>/dev/null
    fi
}

# Talk to the daemon over its unix socket. Echoes the (raw) response line.
_aizsh_via_socket() {
    local frame="$1" fd reply
    [[ -S "$AIZSH_SOCK" ]] || return 1
    zmodload zsh/net/socket 2>/dev/null || return 1
    zsocket "$AIZSH_SOCK" 2>/dev/null || return 1
    fd=$REPLY
    print -u $fd -r -- "$frame"
    # timeout guards against a wedged daemon hanging `prompt` forever; the default
    # comfortably exceeds the backend's THINK_TIMEOUT (120s) for thinking prompts.
    IFS= read -r -t "${AIZSH_READ_TIMEOUT:-130}" -u $fd reply
    local rc=$?
    exec {fd}>&- 2>/dev/null
    (( rc == 0 )) && [[ -n "$reply" ]] && print -r -- "$reply"
}

# Send a request (socket, else one-shot python) and echo the decoded JSON.
# $1 = mode (ghost|prompt|stats|…), $2 = text, $3 = optional field-4 override
# (stats/record send the previous command there instead of history context).
_aizsh_request() {
    emulate -L zsh
    local mode="$1" text="$2" resp
    # Reuse the per-prompt PWD/history encodings cached by precmd (inherited via fork);
    # fall back to computing them if precmd hasn't run yet. Only the buffer is encoded
    # per keystroke. (`-` not `:-` for hist so an empty history isn't re-encoded.)
    local pwd_b64="${_AIZSH_PWD_B64:-$(_aizsh_b64 "$PWD")}"
    local extra_b64
    if (( $# >= 3 )); then
        extra_b64="$(_aizsh_b64 "$3")"
    else
        extra_b64="${_AIZSH_HIST_B64-$(_aizsh_b64 "$(fc -ln -${AIZSH_HIST_N} 2>/dev/null)")}"
    fi
    local frame="${mode}"$'\x1f'"$(_aizsh_b64 "$text")"$'\x1f'"${pwd_b64}"$'\x1f'"${extra_b64}"
    resp="$(_aizsh_via_socket "$frame")"
    if [[ -z "$resp" ]]; then
        resp="$(print -r -- "$frame" | "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" oneshot 2>/dev/null)"
        _aizsh_start_daemon   # bring it up for next time
    fi
    [[ -n "$resp" ]] || return 1
    _aizsh_b64d "$resp"
}

# Fetch with a spinner; ⌃C skips. $1=mode $2=text $3=label. Echoes decoded JSON.
_aizsh_fetch_spin() {
    emulate -L zsh
    setopt localoptions no_notify
    local mode="$1" text="$2" label="${3:-thinking}" tmp out
    local DIM=$'\e[2m' RST=$'\e[0m'
    tmp="$(mktemp -t aizsh.XXXXXX)" || return 1
    { _aizsh_request "$mode" "$text" >| "$tmp" 2>/dev/null } &!
    local pid=$! skipped= i=1
    local chars=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    trap 'skipped=1' INT
    local key
    # spinner goes to STDERR so it never pollutes the captured JSON on stdout
    while kill -0 $pid 2>/dev/null; do
        [[ -n $skipped ]] && { kill $pid 2>/dev/null; break; }
        printf '\r%s%s %s… (Enter to dismiss)%s' "$DIM" "${chars[i]}" "$label" "$RST" >&2
        i=$(( i % 10 + 1 ))
        # read a key for up to 0.1s (doubles as the frame delay); Enter cancels
        if read -t 0.1 -k 1 -s key 2>/dev/null; then
            [[ "$key" == $'\n' || "$key" == $'\r' ]] && { kill $pid 2>/dev/null; skipped=1; break; }
        fi
    done
    trap - INT
    printf '\r\e[K' >&2
    [[ -n $skipped ]] && { rm -f "$tmp"; return 1; }
    out="$(<"$tmp")"; rm -f "$tmp"
    [[ -n $out ]] || return 1
    print -r -- "$out"
}

# --- daemon lifecycle -------------------------------------------------
_aizsh_daemon_alive() {
    [[ -S "$AIZSH_SOCK" ]] || return 1
    [[ "$(_aizsh_via_socket ping)" == pong* ]]
}

_aizsh_start_daemon() {
    _aizsh_have_ai || return
    (( $+commands[${AIZSH_PYTHON%% *}] )) || return
    local lock="${AIZSH_SOCK}.lock"
    if mkdir "$lock" 2>/dev/null; then           # atomic: only one starter wins
        ( nohup "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" daemon >>"$AIZSH_LOG" 2>&1 & ) &!
        ( command sleep 3; rmdir "$lock" 2>/dev/null ) &!
    fi
}

# --- AI suggestion strategy (runs inside autosuggestions' async worker) ---
# The async worker is killed on the next keystroke, so the leading sleep gives
# us free trailing-edge debouncing: only a real pause survives to hit the API.
_zsh_autosuggest_strategy_ai() {
    emulate -L zsh
    local buffer="$1"
    _aizsh_have_ai || return
    if [[ -z "$buffer" ]]; then
        # empty prompt: a just-failed command's fix takes priority over a generic
        # prediction; both render as grey ghost text (Tab accepts, Enter dismisses).
        if [[ -n "$_AIZSH_FIX_CMD" ]]; then
            command sleep "$AIZSH_DEBOUNCE"
            local fix
            fix="$(_aizsh_request fix "${_AIZSH_FIX_EC}"$'\x1e'"${_AIZSH_FIX_CMD}" | _aizsh_json_get command)"
            [[ -n "$fix" ]] && typeset -g suggestion="$fix"
            return
        fi
        [[ "$AIZSH_PREDICT" == 1 ]] || return
        command sleep "$AIZSH_DEBOUNCE"
        local pred
        pred="$(_aizsh_request predict "" | _aizsh_json_get suggestion)"
        [[ -n "$pred" ]] && typeset -g suggestion="$pred"
        return
    fi
    (( ${#buffer} >= AIZSH_MIN_LEN )) || return
    local first=${buffer%%[[:space:]]*}
    [[ "$first" == (prompt|ai|ask) ]] && return       # NL line, not a command
    command sleep "$AIZSH_DEBOUNCE"
    local suffix
    suffix="$(_aizsh_request ghost "$buffer" | _aizsh_json_get suggestion)"
    [[ -n "$suffix" ]] || return
    typeset -g suggestion="${buffer}${suffix}"
}

# --- empty-prompt next-command prediction -----------------------------
# Reuse autosuggestions' async + highlight + accept machinery, lifting its two
# empty-buffer guards so a prediction can render on a blank line. These override
# functions defined when zsh-autosuggestions was sourced (before this plugin).

# (1) allow a suggestion to render even when the buffer is empty
_zsh_autosuggest_suggest() {
    emulate -L zsh
    local suggestion="$1"
    if [[ -n "$suggestion" ]]; then
        POSTDISPLAY="${suggestion#$BUFFER}"
    else
        POSTDISPLAY=
    fi
}

# (2) on an empty buffer, only the AI predict strategy runs (it may decline);
#     never fall back to history, which would just echo the last command
_zsh_autosuggest_fetch_suggestion() {
    typeset -g suggestion
    local -a strategies
    local strategy
    if [[ -z "$1" ]]; then
        # empty buffer → AI only (history would just echo the last command). A pending
        # fix is AI-only; otherwise predict via AI then fall back to the stats guess.
        if [[ -n "$_AIZSH_FIX_CMD" ]]; then
            strategies=(ai)
        elif [[ "$AIZSH_PREDICT" == 1 ]]; then
            strategies=(ai stats)
        else
            strategies=()
        fi
    else
        strategies=(${=ZSH_AUTOSUGGEST_STRATEGY})
    fi
    for strategy in $strategies; do
        _zsh_autosuggest_strategy_$strategy "$1"
        [[ "$suggestion" != "$1"* ]] && unset suggestion
        [[ -n "$suggestion" ]] && break
    done
}

# --- statistical layer: instant local guess (frecency/dir/sequence) ----
# Socket-only (never one-shot — that would fork python per keystroke and read an
# empty store); short timeout so a slow/missing daemon never blocks typing.
_aizsh_stats_lookup() {   # $1 = buffer; echoes the full statistical suggestion
    [[ -S "$AIZSH_SOCK" ]] || return 1
    local frame resp
    frame="stats"$'\x1f'"$(_aizsh_b64 "$1")"$'\x1f'"${_AIZSH_PWD_B64:-$(_aizsh_b64 "$PWD")}"$'\x1f'"$(_aizsh_b64 "$AIZSH_PREV_CMD")"
    resp="$(AIZSH_READ_TIMEOUT=0.3 _aizsh_via_socket "$frame")" || return 1
    [[ -n "$resp" ]] && _aizsh_b64d "$resp" | _aizsh_json_get suggestion
}

# stats strategy (async fallback): used when the LLM is empty/slow/offline
_zsh_autosuggest_strategy_stats() {
    emulate -L zsh
    [[ "$AIZSH_STATS" == 1 ]] || return
    local buffer="$1" full
    if [[ -n "$buffer" ]]; then
        (( ${#buffer} >= AIZSH_MIN_LEN )) || return
        [[ "${buffer%%[[:space:]]*}" == (prompt|ai|ask) ]] && return
    fi
    full="$(_aizsh_stats_lookup "$buffer")"
    [[ -n "$full" && "$full" == "$buffer"* ]] && typeset -g suggestion="$full"
}

# Override _zsh_autosuggest_fetch: render the INSTANT statistical guess synchronously,
# THEN kick off the async LLM fetch which refines/replaces it. This is what makes the
# ghost feel instant despite model latency. (modify already cleared POSTDISPLAY and
# handled type-through, so we only run when an actual fetch is needed.)
_zsh_autosuggest_fetch() {
    if [[ "$AIZSH_STATS" == 1 ]]; then
        local _full=
        if [[ -n "$BUFFER" ]]; then
            if (( ${#BUFFER} >= AIZSH_MIN_LEN )) && [[ "${BUFFER%%[[:space:]]*}" != (prompt|ai|ask) ]]; then
                _full="$(_aizsh_stats_lookup "$BUFFER")"
            fi
        elif [[ -z "$_AIZSH_FIX_CMD" && "$AIZSH_PREDICT" == 1 ]]; then
            _full="$(_aizsh_stats_lookup "")"
        fi
        [[ -n "$_full" && "$_full" == "$BUFFER"* ]] && POSTDISPLAY="${_full#$BUFFER}"
    fi
    if (( ${+ZSH_AUTOSUGGEST_USE_ASYNC} )); then
        _zsh_autosuggest_async_request "$BUFFER"
    else
        local suggestion
        _zsh_autosuggest_fetch_suggestion "$BUFFER"
        _zsh_autosuggest_suggest "$suggestion"
    fi
}

# (3) kick off a fix/prediction when a fresh (empty) prompt line appears;
#     the result renders as grey ghost text on the prompt when ready (no spinner).
_aizsh_line_init() {
    [[ -z "$BUFFER" ]] || return
    _aizsh_have_ai || return
    if [[ -n "$_AIZSH_FIX_CMD" || "$AIZSH_PREDICT" == 1 ]]; then
        _zsh_autosuggest_fetch
        _AIZSH_FIX_CMD=    # consumed; the async worker already forked with its copy
    fi
}
autoload -Uz add-zle-hook-widget
add-zle-hook-widget line-init _aizsh_line_init 2>/dev/null

# --- Ctrl-O: force an AI suggestion right now (bypass debounce/gating) ---
_aizsh_force_widget() {
    emulate -L zsh
    [[ -n "$BUFFER" ]] && _aizsh_have_ai || return
    (( $+functions[_zsh_autosuggest_suggest] )) || return
    zle -R "🤖 thinking…"
    local suffix
    suffix="$(_aizsh_request ghost "$BUFFER" | _aizsh_json_get suggestion)"
    [[ -n "$suffix" ]] && _zsh_autosuggest_suggest "${BUFFER}${suffix}"
    zle -R
}
zle -N _aizsh_force_widget
bindkey "$AIZSH_FORCE_KEY" _aizsh_force_widget

# --- word-by-word acceptance: take the next word of the ghost, leave the rest ---
# zsh-autosuggestions already does partial-accept on `forward-word`; we just give it
# a discoverable key (Ctrl-Right by default). Also keeps Ctrl-Right's normal job.
bindkey "$AIZSH_WORD_ACCEPT_KEY" forward-word

# --- `prompt <natural language>` -> command, with editable approval -----
prompt() {
    emulate -L zsh
    local query="$*"
    if [[ -z "$query" ]]; then
        print -u2 "usage: prompt <what you want>    e.g.  prompt list git branches by last commit date"
        return 1
    fi
    if ! _aizsh_have_ai; then
        print -u2 "ai-zsh: no AI provider (set AIZSH_PROVIDER=ollama, or GEMINI_API_KEY)."
        return 1
    fi

    local json
    json="$(_aizsh_fetch_spin prompt "$query" thinking)" || return 1
    [[ -n "$json" ]] || { print -u2 "ai-zsh: backend unavailable (need $AIZSH_PYTHON + $AIZSH_BACKEND)"; return 1; }

    local cmd expl danger
    cmd="$(print -r -- "$json"    | _aizsh_json_get command)"
    expl="$(print -r -- "$json"   | _aizsh_json_get explanation)"
    danger="$(print -r -- "$json" | _aizsh_json_get danger)"
    if [[ -z "$cmd" ]]; then
        print -u2 "ai-zsh: ${expl:-no command returned}"; return 1
    fi

    # Offer alternatives via fzf when there are any and fzf is installed.
    local -a alts
    alts=("${(@f)$(print -r -- "$json" | { (( $+commands[jq] )) && jq -r '.alternatives[]? // empty' 2>/dev/null; })}")
    alts=(${(M)alts:#?*})
    local chosen="$cmd"
    if (( ${#alts} )) && (( $+commands[fzf] )); then
        local TAB=$'\t' picked
        picked="$({
            print -r -- "${cmd}${TAB}${expl}"
            local a; for a in "${alts[@]}"; do print -r -- "${a}${TAB}(alternative)"; done
        } | fzf --height='~45%' --reverse --no-sort --prompt='run> ' \
                --delimiter=$'\t' --with-nth='1,2' \
                --header='↑↓ choose · Enter pre-fill · Esc cancel')"
        [[ -z "$picked" ]] && { print -u2 "cancelled"; return 130; }
        chosen="${picked%%$TAB*}"
    fi

    # Danger gate. Nothing ever auto-runs: we only PRE-FILL the line for review.
    local DIM=$'\e[2m' RST=$'\e[0m' YEL=$'\e[33m' RED=$'\e[1;31m'
    case "$danger" in
        dangerous)
            print -r -- "${RED}⛔ DANGEROUS${RST} ${DIM}${expl}${RST}"
            print -r -- "   ${chosen}"
            if ! read -q "REPLY?   pre-fill this command for review anyway? [y/N] "; then
                print; print -u2 "aborted"; return 1
            fi
            print
            ;;
        caution)
            print -r -- "${YEL}⚠ caution${RST}  ${DIM}${expl}${RST}" ;;
        *)
            [[ -n "$expl" ]] && print -r -- "${DIM}↳ ${expl}${RST}" ;;
    esac
    print -z -- "$chosen"     # appears at the next prompt, editable; Enter to run
}
alias ai='prompt'
alias ask='prompt'

# --- management command ----------------------------------------------
aizsh() {
    emulate -L zsh
    case "${1:-status}" in
        status)
            if _aizsh_daemon_alive; then print "● daemon running   $AIZSH_SOCK"
            else print "○ daemon not running"; fi
            print "  provider    : ${AIZSH_PROVIDER:-auto-detect}"
            print "  ghost model : ${AIZSH_GHOST_MODEL:-(provider default)}"
            print "  prompt model: ${AIZSH_PROMPT_MODEL:-(provider default)}"
            print "  strategy    : ${ZSH_AUTOSUGGEST_STRATEGY[*]}"
            print "  auto-fix    : ${AIZSH_AUTOFIX:-1}"
            print "  predict     : ${AIZSH_PREDICT:-1}"
            print "  log         : $AIZSH_LOG"
            print "  (run 'aizsh doctor' for a live provider/model check)"
            ;;
        start)    _aizsh_start_daemon; print "starting daemon…" ;;
        stop)     "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" stop ;;
        restart)  "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" stop >/dev/null 2>&1
                  rmdir "${AIZSH_SOCK}.lock" 2>/dev/null
                  _aizsh_start_daemon; print "restarted" ;;
        log|logs) ${PAGER:-cat} "$AIZSH_LOG" ;;
        tail)     tail -f "$AIZSH_LOG" ;;
        doctor)   "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" doctor ;;
        test)     "${=AIZSH_PYTHON}" "$AIZSH_BACKEND" ${2:-ghost} "${3:-git ch}" "$PWD" ;;
        on)       ZSH_AUTOSUGGEST_STRATEGY=(ai history); print "AI ghost text: ON" ;;
        off)      ZSH_AUTOSUGGEST_STRATEGY=(history);     print "AI ghost text: OFF (history only)" ;;
        autofix)
            case "$2" in
                on)  AIZSH_AUTOFIX=1; print "auto-fix: ON" ;;
                off) AIZSH_AUTOFIX=0; print "auto-fix: OFF" ;;
                *)   print "auto-fix is ${AIZSH_AUTOFIX:-1} — use: aizsh autofix on|off" ;;
            esac ;;
        predict)
            case "$2" in
                on)  AIZSH_PREDICT=1; print "next-command prediction: ON" ;;
                off) AIZSH_PREDICT=0; print "next-command prediction: OFF" ;;
                *)   print "predict is ${AIZSH_PREDICT:-1} — use: aizsh predict on|off" ;;
            esac ;;
        *) print "usage: aizsh {status|start|stop|restart|log|tail|doctor|test|on|off|autofix|predict}" ;;
    esac
}

# --- automatic AI fix for failed commands ----------------------------
# When a command fails (or isn't found), the corrected command is shown as grey
# GHOST TEXT on the next prompt — Tab to accept, Enter to dismiss. We just stash
# the failure here; the empty-prompt `ai` strategy (above) renders it, taking
# priority over a generic prediction.
: ${AIZSH_AUTOFIX:=1}              # suggest a fix (grey ghost text) when a command fails
# leading commands where a non-zero exit is normal — never offer a fix for these
: ${AIZSH_AUTOFIX_SKIP:="grep egrep fgrep rg ag ack diff colordiff cmp test pgrep pkill which type whence man less more fzf ssh nvim vim vi nano ping"}

_aizsh_preexec() { AIZSH_LAST_CMD="$1"; AIZSH_RAN=1; _AIZSH_EXEC_PWD="$PWD"; }

# Stash a fix request; the next empty prompt turns it into grey ghost text.
_aizsh_stash_fix() { typeset -g _AIZSH_FIX_EC="$1" _AIZSH_FIX_CMD="$2"; }

# Feed a finished command into the daemon's statistical layer (socket-only).
_aizsh_record() {   # $1 = command, $2 = "<exit>\x1e<prev command>"
    [[ "$AIZSH_STATS" == 1 ]] || return
    [[ -S "$AIZSH_SOCK" ]] || return
    local frame
    frame="record"$'\x1f'"$(_aizsh_b64 "$1")"$'\x1f'"$(_aizsh_b64 "${_AIZSH_EXEC_PWD:-$PWD}")"$'\x1f'"$(_aizsh_b64 "$2")"
    AIZSH_READ_TIMEOUT=0.3 _aizsh_via_socket "$frame" >/dev/null 2>&1
}

_aizsh_precmd() {
    local ec=$?
    # Pre-encode PWD + recent history ONCE per prompt (they only change on cd / a new
    # command). Per-keystroke ghost workers are forks of this shell, so they inherit
    # these and skip re-spawning base64/fc on every key. (Done before the early returns
    # so it runs on every prompt; $? is already saved in `ec`.)
    if _aizsh_have_ai; then
        typeset -g _AIZSH_PWD_B64="$(_aizsh_b64 "$PWD")"
        typeset -g _AIZSH_HIST_B64="$(_aizsh_b64 "$(fc -ln -${AIZSH_HIST_N} 2>/dev/null)")"
    fi
    [[ -n $AIZSH_RAN ]] || return            # nothing actually ran (empty prompt)
    AIZSH_RAN=
    # record the command into the statistical layer, then remember it as `prev`
    _aizsh_record "$AIZSH_LAST_CMD" "${ec}"$'\x1e'"${_AIZSH_PREV_CMD}"
    typeset -g _AIZSH_PREV_CMD="$AIZSH_LAST_CMD"
    [[ "$AIZSH_AUTOFIX" == 1 ]] || return
    _aizsh_have_ai || return
    (( ec == 0 ))   && return
    (( ec >= 128 )) && return                # signals: Ctrl-C (130), SIGTERM, etc.
    # ec 127 = command not found — handled here (the command_not_found_handler runs
    # in a subshell, so its typeset -g can't reach us; precmd is the main shell).
    local first=${${AIZSH_LAST_CMD%%[[:space:]]*}:t}
    [[ -n $first ]] || return
    [[ " $AIZSH_AUTOFIX_SKIP " == *" $first "* ]] && return
    [[ $first == (prompt|ai|ask|aizsh) ]] && return
    _aizsh_stash_fix "$ec" "$AIZSH_LAST_CMD"
}

# Note: we deliberately do NOT define command_not_found_handler — zsh runs it in a
# subshell, so it can't stash state for the next prompt. precmd (main shell) catches
# exit 127 instead, and zsh prints its own "command not found" message.

autoload -Uz add-zsh-hook
add-zsh-hook preexec _aizsh_preexec
add-zsh-hook precmd  _aizsh_precmd
# run our precmd FIRST so it sees the real $? before other hooks clobber it
precmd_functions=(_aizsh_precmd ${precmd_functions:#_aizsh_precmd})

# --- bring the daemon up (cheap: pings first, only starts if needed) ---
_aizsh_daemon_alive || _aizsh_start_daemon
