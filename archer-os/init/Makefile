# archer_init Makefile
#
# Builds a statically linked binary — no shared library dependencies.
# This is required for PID 1 because /usr/lib may not be available at boot.
#
# Usage:
#   make              → builds archer_init
#   make clean        → removes built files
#   make check        → verifies the binary is statically linked
#
# The binary is intentionally small (~50KB). -Os optimizes for size.

CC      = gcc
CFLAGS  = -static -Os -Wall -Wextra -std=c11 \
           -D_GNU_SOURCE \
           -fstack-protector-strong \
           -fno-common
LDFLAGS = -static

TARGET  = archer_init
SRC     = archer_init.c

.PHONY: all clean check

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)
	@echo "Built: $@ ($(shell stat -c%s $@) bytes)"

clean:
	rm -f $(TARGET)

check: $(TARGET)
	@echo "=== Checking binary is statically linked ==="
	file $(TARGET)
	ldd $(TARGET) 2>&1 || true
	@echo "=== Size ==="
	ls -lh $(TARGET)
	@echo "=== Symbols (should be minimal) ==="
	nm $(TARGET) 2>/dev/null | wc -l || true
