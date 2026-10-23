# venv.sh - create (first run) and activate the Python venv for bp3_bdm.py
#
# Must be SOURCED so the activation sticks in your shell:
#     source ./venv.sh        (or:  . ./venv.sh)
#
# First run: creates .venv next to this script and installs pyserial.
# Later runs: just activates it.

# Refuse to run as a subprocess - activation would be lost when it exits.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "Run this with:  source ${0}" >&2
    exit 1
fi

_gn30_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_gn30_venv="$_gn30_dir/.venv"

if [[ ! -f "$_gn30_venv/bin/activate" ]]; then
    echo "Creating virtual environment in $_gn30_venv ..."
    if ! python3 -m venv "$_gn30_venv"; then
        echo "Failed to create venv. On Debian/Ubuntu: sudo apt install python3-venv" >&2
        unset _gn30_dir _gn30_venv
        return 1
    fi
    # shellcheck disable=SC1091
    source "$_gn30_venv/bin/activate"
    python -m pip install --upgrade pip >/dev/null
    if ! python -m pip install pyserial; then
        echo "pip install failed; removing incomplete venv." >&2
        deactivate
        rm -rf "$_gn30_venv"
        unset _gn30_dir _gn30_venv
        return 1
    fi
    echo "Venv ready."
else
    # shellcheck disable=SC1091
    source "$_gn30_venv/bin/activate"
fi

echo "Activated: $VIRTUAL_ENV  ($(python --version 2>&1))"

# Serial port access hint
if [[ $EUID -ne 0 ]] && ! id -nG | grep -qwE 'dialout|uucp'; then
    echo "Note: you're not in the 'dialout' group; /dev/ttyUSB* may need:"
    echo "      sudo usermod -aG dialout \$USER   (then log out and back in)"
fi

unset _gn30_dir _gn30_venv
