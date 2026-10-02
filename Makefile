# Builds the C++ engine and runs every test.
# Windows: use `mingw32-make` (ships with MSYS2) in place of `make`.

CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra
HEADERS  := engine/include/exchange.hpp

ifeq ($(OS),Windows_NT)
  EXE := .exe
  # Link the C++ runtime in, so an older libstdc++ DLL earlier on PATH
  # (Git for Windows ships one) cannot be loaded by mistake.
  LDFLAGS += -static
endif

ENGINE := build/engine_server$(EXE)
TESTS  := build/test_exchange$(EXE)

.PHONY: all engine test test-cpp test-py clean

all: engine

engine: $(ENGINE)

$(ENGINE): engine/src/engine_server.cpp $(HEADERS)
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

$(TESTS): engine/tests/test_exchange.cpp $(HEADERS)
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDFLAGS)

test-cpp: $(TESTS)
	./$(TESTS)

test-py: $(ENGINE)
	uv run pytest -q

test: test-cpp test-py

clean:
	rm -rf build
