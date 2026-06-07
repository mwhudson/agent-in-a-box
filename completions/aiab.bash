# Bash completion for aiab.
#
# Source it from your shell startup, e.g. add to ~/.bashrc:
#     source /path/to/agent-in-a-box/completions/aiab.bash
# or symlink it into a bash-completion directory (e.g.
# ~/.local/share/bash-completion/completions/aiab).
#
# Directory completion uses _filedir from the bash-completion package when
# available, falling back to plain `compgen -d`.

_aiab() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local subcommands="run remove mount unmount upgrade-templates list lxc"
    local agents="claude claude-or opencode copilot"

    _aiab_dirs() {
        if declare -F _filedir >/dev/null 2>&1; then
            _filedir -d
        else
            COMPREPLY+=( $(compgen -d -- "$cur") )
        fi
    }

    # Locate the subcommand: the first non-option word after `aiab`.
    local sub="" i
    for (( i=1; i < COMP_CWORD; i++ )); do
        case "${COMP_WORDS[i]}" in
            -*) ;;
            *) sub="${COMP_WORDS[i]}"; break ;;
        esac
    done

    if [[ -z "$sub" ]]; then
        COMPREPLY=( $(compgen -W "$subcommands" -- "$cur") )
        return
    fi

    if [[ "$prev" == "--for" ]]; then
        _aiab_dirs
        return
    fi

    case "$sub" in
        run)
            COMPREPLY=( $(compgen -W "$agents --for --add-mount --add-mount-rw --shell" -- "$cur") )
            _aiab_dirs
            ;;
        remove)
            COMPREPLY=( $(compgen -W "$agents --for" -- "$cur") )
            ;;
        upgrade-templates)
            COMPREPLY=( $(compgen -W "$agents" -- "$cur") )
            ;;
        mount)
            COMPREPLY=( $(compgen -W "--for --ro --rw" -- "$cur") )
            _aiab_dirs
            ;;
        unmount)
            COMPREPLY=( $(compgen -W "--for" -- "$cur") )
            _aiab_dirs
            ;;
        list)
            COMPREPLY=( $(compgen -W "--for" -- "$cur") )
            ;;
        lxc)
            ;;  # defer to whatever the user types for lxc
    esac
}

complete -F _aiab aiab
