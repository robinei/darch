#!/bin/bash
# Simple QEMU test script for arch-atomic images

IMAGE="${1:-arch-test.img}"

if [ ! -f "$IMAGE" ]; then
    echo "Error: Image file '$IMAGE' not found"
    exit 1
fi

# Check for qemu
if ! command -v qemu-system-x86_64 &> /dev/null; then
    echo "Error: qemu-system-x86_64 not found"
    echo "Install with: sudo pacman -S qemu-full"
    exit 1
fi

# Try to find OVMF files (check both naming conventions)
OVMF_CODE="/usr/share/edk2-ovmf/x64/OVMF_CODE.4m.fd"
OVMF_VARS="/usr/share/edk2-ovmf/x64/OVMF_VARS.4m.fd"

if [ ! -f "$OVMF_CODE" ]; then
    OVMF_CODE="/usr/share/edk2-ovmf/x64/OVMF_CODE.fd"
fi

if [ ! -f "$OVMF_VARS" ]; then
    OVMF_VARS="/usr/share/edk2-ovmf/x64/OVMF_VARS.fd"
fi

if [ ! -f "$OVMF_CODE" ] || [ ! -f "$OVMF_VARS" ]; then
    echo "Error: OVMF firmware not found"
    echo "Install with: sudo pacman -S edk2-ovmf"
    exit 1
fi

echo "Starting QEMU with image: $IMAGE"
echo "OVMF: $OVMF_CODE"

# Create a copy of OVMF_VARS for this session (it's writable)
VARS_COPY="$(mktemp)"
cp "$OVMF_VARS" "$VARS_COPY"

LOGFILE="qemu-console.log"
echo "Logging console output to: $LOGFILE"
echo "Exit with: Ctrl-A X"
echo ""

qemu-system-x86_64 \
    -enable-kvm \
    -cpu host \
    -m 4G \
    -smp 2 \
    -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
    -drive if=pflash,format=raw,file="$VARS_COPY" \
    -drive file="$IMAGE",format=raw \
    -net none \
    -nographic \
    -chardev stdio,mux=on,id=char0,logfile="$LOGFILE",signal=off \
    -serial chardev:char0 \
    -mon chardev=char0

# Cleanup
rm -f "$VARS_COPY"
