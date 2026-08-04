    FILE=${lib.escapeShellArg (userEnvFile name)}
    if [ -r "$FILE" ]; then
      while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ('#'*|"") continue ;; (*=*) ;; (*) continue ;; esac
        key=''${line%%=*}
        case "$key" in (*[!A-Za-z0-9_]*|""|[0-9]*) continue ;; esac
        val=''${line#*=}
        case "$val" in
          \"*\") val=''${val#\"}; val=''${val%\"} ;;
          \'*\') val=''${val#\'}; val=''${val%\'} ;;
        esac
        export "$key=$val"
      done < "$FILE"
    fi
    exec "$@"
