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
export AIZSH_SOCK             # backend (daemon + oneshot) must use the same path

# AI ghost text needs async, otherwise every keystroke would block on the network.
ZSH_AUTOSUGGEST_USE_ASYNC=1
# AI-first, history-aware: try AI, fall back to local history instantly if AI is
# empty / rate-limited / offline.  (Leave a user's explicit non-default alone.)
typeset -ga ZSH_AUTOSUGGEST_STRATEGY
if [[ ${#ZSH_AUTOSUGGEST_STRATEGY} -eq 0 || "${ZSH_AUTOSUGGEST_STRATEGY[*]}" == "history" ]]; then
    ZSH_AUTOSUGGEST_STRATEGY=(ai history)
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
    IFS= read -r -u $fd reply
    local rc=$?
    exec {fd}>&- 2>/dev/null
    (( rc == 0 )) && [[ -n "$reply" ]] && print -r -- "$reply"
}

# Send a request (socket, else one-shot python) and echo the decoded JSON.
# $1 = mode (ghost|prompt), $2 = text
_aizsh_request() {
    emulate -L zsh
    local mode="$1" text="$2" hist resp
    hist="$(fc -ln -${AIZSH_HIST_N} 2>/dev/null)"
    local frame="${mode}"$'\x1f'"$(_aizsh_b64 "$text")"$'\x1f'"$(_aizsh_b64 "$PWD")"$'\x1f'"$(_aizsh_b64 "$hist")"
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
    while kill -0 $pid 2>/dev/null; do
        [[ -n $skipped ]] && { kill $pid 2>/dev/null; break; }
        printf '\r%s%s %s… (⌃C to skip)%s' "$DIM" "${chars[i]}" "$label" "$RST"
        i=$(( i % 10 + 1 )); command sleep 0.1
    done
    trap - INT
    printf '\r\e[K'
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
        # empty prompt → predict the likely next command (model may decline → empty)
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
        [[ "$AIZSH_PREDICT" == 1 ]] && strategies=(ai) || strategies=()
    else
        strategies=(${=ZSH_AUTOSUGGEST_STRATEGY})
    fi
    for strategy in $strategies; do
        _zsh_autosuggest_strategy_$strategy "$1"
        [[ "$suggestion" != "$1"* ]] && unset suggestion
        [[ -n "$suggestion" ]] && break
    done
}

# (3) kick off a prediction when a fresh (empty) prompt line appears
_aizsh_line_init() {
    [[ "$AIZSH_PREDICT" == 1 && -z "$BUFFER" ]] && _aizsh_have_ai && _zsh_autosuggest_fetch
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
: ${AIZSH_AUTOFIX:=1}              # auto-suggest a fix when a command fails
: ${AIZSH_AUTOFIX_PREFILL:=1}     # pre-fill the fix on the next prompt (vs just print it)
# leading commands where a non-zero exit is normal — never offer a fix for these
: ${AIZSH_AUTOFIX_SKIP:="grep egrep fgrep rg ag ack diff colordiff cmp test pgrep pkill which type whence man less more fzf ssh nvim vim vi nano ping"}

_aizsh_preexec() { AIZSH_LAST_CMD="$1"; AIZSH_RAN=1; }

# $1 = exit code, $2 = failed command. Fetches a fix (spinner, ⌃C to skip) and
# pre-fills it for review. Never runs anything.
_aizsh_doctor() {
    emulate -L zsh
    local ec="$1" cmd="$2" json fixcmd expl danger
    local DIM=$'\e[2m' RST=$'\e[0m' CYN=$'\e[36m' YEL=$'\e[33m' RED=$'\e[1;31m'
    json="$(_aizsh_fetch_spin fix "${ec}"$'\x1e'"${cmd}" ai-fix)" || return
    fixcmd="$(print -r -- "$json" | _aizsh_json_get command)"
    [[ -n $fixcmd ]] || return
    expl="$(print -r -- "$json" | _aizsh_json_get explanation)"
    danger="$(print -r -- "$json" | _aizsh_json_get danger)"
    print -r -- "${CYN}✦ ai-fix${RST} ${DIM}${expl}${RST}"
    case "$danger" in
        dangerous) print -r -- "  ${RED}⛔ dangerous — review carefully${RST}" ;;
        caution)   print -r -- "  ${YEL}⚠ caution${RST}" ;;
    esac
    if [[ "$AIZSH_AUTOFIX_PREFILL" == 1 ]]; then
        print -z -- "$fixcmd"
    else
        print -r -- "  ${fixcmd}"
    fi
}

_aizsh_precmd() {
    local ec=$?
    [[ -n $AIZSH_RAN ]] || return            # nothing actually ran (empty prompt)
    AIZSH_RAN=
    [[ "$AIZSH_AUTOFIX" == 1 ]] || return
    (( ec == 0 ))   && return
    (( ec >= 128 )) && return                # signals: Ctrl-C (130), SIGTERM, etc.
    (( ec == 127 )) && return                # owned by command_not_found_handler
    local first=${${AIZSH_LAST_CMD%%[[:space:]]*}:t}
    [[ -n $first ]] || return
    [[ " $AIZSH_AUTOFIX_SKIP " == *" $first "* ]] && return
    [[ $first == (prompt|ai|ask|aizsh) ]] && return
    _aizsh_doctor "$ec" "$AIZSH_LAST_CMD"
}

command_not_found_handler() {
    local cmd="$*"
    AIZSH_RAN=                               # don't double-handle from precmd
    print -u2 "zsh: command not found: $1"
    [[ "$AIZSH_AUTOFIX" == 1 ]] && _aizsh_doctor 127 "$cmd"
    return 127
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _aizsh_preexec
add-zsh-hook precmd  _aizsh_precmd
# run our precmd FIRST so it sees the real $? before other hooks clobber it
precmd_functions=(_aizsh_precmd ${precmd_functions:#_aizsh_precmd})

# --- bring the daemon up (cheap: pings first, only starts if needed) ---
_aizsh_daemon_alive || _aizsh_start_daemon
