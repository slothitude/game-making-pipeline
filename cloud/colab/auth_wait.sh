#!/bin/bash
# Colab CLI auth waiter — holds a fifo so the code can be delivered later.
export PATH="$HOME/.local/bin:$PATH"
rm -f /tmp/colab-code /tmp/colab-auth.log
mkfifo /tmp/colab-code
# dummy writer unblocks the fifo read-open so colab prints its URL now
sleep 86400 > /tmp/colab-code &
HOLDER=$!
colab whoami < /tmp/colab-code > /tmp/colab-auth.log 2>&1
kill "$HOLDER" 2>/dev/null
