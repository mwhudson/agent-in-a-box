#compdef aiab
# Zsh completion for aiab.
#
# Put this file on your $fpath as `_aiab` (e.g. symlink it into a directory
# listed in fpath, ahead of compinit), or source it directly.

_aiab() {
    local -a subcommands agents
    subcommands=(
        'run:run an agent in a container for the current directory'
        'remove:delete the session container for a directory'
        'mount:mount extra directories into a directory'\''s containers'
        'unmount:remove extra directory mounts'
        'upgrade-templates:apt upgrade + reinstall agents in templates'
        'list:list aiab containers'
        'lxc:run lxc against the aiab project'
    )
    agents=(claude claude-or opencode copilot)

    if (( CURRENT == 2 )); then
        _describe -t commands 'aiab command' subcommands
        return
    fi

    case "${words[2]}" in
        run)
            _arguments \
                '--also[mount DIR read-only]:directory:_files -/' \
                '--also-rw[mount DIR read-write]:directory:_files -/' \
                '--shell[open a shell instead of the agent]' \
                "1:agent:(${agents})"
            ;;
        remove)
            _arguments \
                '--for[target the container for DIR]:directory:_files -/' \
                "1:agent:(${agents})"
            ;;
        upgrade-templates)
            _values 'agent' $agents
            ;;
        mount)
            _arguments \
                '--for[target the containers for DIR]:directory:_files -/' \
                '--ro[mount read-only]' \
                '--rw[mount read-write]' \
                '*:directory:_files -/'
            ;;
        unmount)
            _arguments \
                '--for[target the containers for DIR]:directory:_files -/' \
                '*:directory:_files -/'
            ;;
        list)
            _arguments '--for[show only the containers for DIR]:directory:_files -/'
            ;;
        lxc)
            ;;
    esac
}

_aiab "$@"
