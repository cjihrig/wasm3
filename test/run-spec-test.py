#!/usr/bin/env python3

# Author: Volodymyr Shymanskyy
# Usage:
#   ./run-spec-test.py
#   ./run-spec-test.py ./core/i32.json
#   ./run-spec-test.py ./core/float_exprs.json --line 2070
#   ./run-spec-test.py ./proposals/tail-call/*.json
#   ./run-spec-test.py --exec "../build-custom/wasm3 --repl"
#
# Running WASI verison with different engines:
#   cp ../build-wasi/wasm3.wasm ./
#   ./run-spec-test.py --exec "../build/wasm3 wasm3.wasm --repl"
#   ./run-spec-test.py --exec "wasmtime --dir=. wasm3.wasm -- --repl"
#   ./run-spec-test.py --exec "wasmer run --dir=. wasm3.wasm -- --repl"
#   ./run-spec-test.py --exec "wasmer run --dir=. --backend=llvm wasm3.wasm -- --repl"
#   ./run-spec-test.py --exec "wasmer-js run wasm3.wasm --dir=. -- --repl"
#   ./run-spec-test.py --exec "wasirun wasm3.wasm --repl"
#   ./run-spec-test.py --exec "wavm run --mount-root ./ wasm3.wasm -- --repl"
#   ./run-spec-test.py --exec "iwasm --dir=. wasm3.wasm --repl"
#

# TODO
# - Get more tests from: https://github.com/microsoft/ChakraCore/tree/master/test/WasmSpec
# - Fix "Empty Stack" check
# - Check Canonical NaN and Arithmetic NaN separately
# - Fix imports.wast

import argparse
import os, sys, glob, time
import subprocess
import json
import re
import struct
import math
import pathlib

scriptDir = os.path.dirname(os.path.abspath(sys.argv[0]))
sys.path.append(os.path.join(scriptDir, '..', 'extra'))

from testutils import *
from pprint import pprint


#
# Args handling
#

parser = argparse.ArgumentParser()
parser.add_argument("--exec", metavar="<interpreter>", default="../build/wasm3 --repl")
parser.add_argument("--timeout", type=int,             default=30)
parser.add_argument("--line", metavar="<source line>", type=int)
parser.add_argument("--all", action="store_true")
parser.add_argument("--show-logs", action="store_true")
parser.add_argument("--format", choices=["raw", "hex", "fp"], default="fp")
parser.add_argument("-v", "--verbose", action="store_true")
parser.add_argument("-s", "--silent", action="store_true")
parser.add_argument("file", nargs='*')

args = parser.parse_args()

if args.line:
    args.show_logs = True

#
# Utilities
#

log = open("spec-test.log","w+")
log.write("======================\n")

def warning(msg):
    log.write("Warning: " + msg + "\n")
    log.flush()
    if args.verbose:
        print(f"{ansi.WARNING}Warning:{ansi.ENDC} {msg}")

def fatal(msg):
    log.write("Fatal: " + msg + "\n")
    log.flush()
    print(f"{ansi.FAIL}Fatal:{ansi.ENDC} {msg}")
    sys.exit(1)

def binaryToFloat(num, t):
    if t == "f32":
        return struct.unpack('!f', struct.pack('!L', int(num)))[0]
    elif t == "f64":
        return struct.unpack('!d', struct.pack('!Q', int(num)))[0]
    else:
        fatal(f"Unknown type '{t}'")

def escape_str(s):
    if s == "":
        return r'\x00'

    if all((ord(c) < 128 and c.isprintable() and not c in " \n\r\t\\") for c in s):
        return s

    return '\\x' + '\\x'.join('{0:02x}'.format(x) for x in s.encode('utf-8'))

#
# Value format options
#

def formatValueRaw(num, t):
    return str(num)

def formatValueHex(num, t):
    if t == "f32" or t == "i32":
        return "{0:#0{1}x}".format(int(num), 8+2)
    elif t == "f64" or t == "i64":
        return "{0:#0{1}x}".format(int(num), 16+2)
    else:
        return str(num)

def formatValueFloat(num, t):
    if t == "f32":
        s = 6
    elif t == "f64":
        s = 10
    else:
        return str(num)

    result = "{0:.{1}f}".format(binaryToFloat(num, t), s).rstrip('0')
    if result.endswith('.'): result = result + '0'
    if len(result) > s*2:
        result = "{0:.{1}e}".format(binaryToFloat(num, t), s)
    return result

formaters = {
    'raw': formatValueRaw,
    'hex': formatValueHex,
    'fp':  formatValueFloat,
}
formatValue = formaters[args.format]

if args.format == "fp":
    print("When using fp display format, values are compared loosely (some tests may produce false positives)")

#
# Spec tests preparation
#

if not (os.path.isdir("./core") and os.path.isdir("./proposals")):
    from io import BytesIO
    from zipfile import ZipFile
    from urllib.request import urlopen

    officialSpec = "https://github.com/wasm3/wasm-core-testsuite/archive/v1.1.zip"

    print(f"Downloading {officialSpec}")
    resp = urlopen(officialSpec)
    with ZipFile(BytesIO(resp.read())) as zipFile:
        for zipInfo in zipFile.infolist():
            if re.match(r".*-.*/.*/.*(\.wasm|\.json)", zipInfo.filename):
                parts = pathlib.Path(zipInfo.filename).parts
                newpath = str(pathlib.Path(*parts[1:-1]))
                newfn   = str(pathlib.Path(*parts[-1:]))
                ensure_path(newpath)
                newpath = newpath + "/" + newfn
                zipInfo.filename = newpath
                zipFile.extract(zipInfo)

#
# Wasm3 REPL
#

from subprocess import Popen, STDOUT, PIPE
from threading import Thread
from queue import Queue, Empty

import shlex

class Wasm3():
    def __init__(self, exe):
        self.exe = exe
        self.p = None
        self.loaded = None
        self.timeout = args.timeout
        self.autorestart = True

        self.run()

    def run(self):
        if self.p:
            self.terminate()

        cmd = shlex.split(self.exe)

        #print(f"wasm3: Starting {' '.join(cmd)}")

        self.q = Queue()
        self.p = Popen(cmd, bufsize=0, stdin=PIPE, stdout=PIPE, stderr=STDOUT)

        def _read_output(out, queue):
            for data in iter(lambda: out.read(1024), b''):
                queue.put(data)
            queue.put(None)

        self.t = Thread(target=_read_output, args=(self.p.stdout, self.q))
        self.t.daemon = True
        self.t.start()

        try:
            self._read_until("wasm3> ")
        except Exception as e:
            print(f"wasm3: Could not start: {e}")

    def restart(self):
        print(f"wasm3: Restarting")
        for i in range(10):
            try:
                self.run()
                try:
                    if self.loaded:
                        self.load(self.loaded)
                except Exception as e:
                    pass
                break
            except Exception as e:
                print(f"wasm3: {e} => retry")
                time.sleep(0.1)

    def init(self):
        return self._run_cmd(f":init\n")

    def version(self):
        return self._run_cmd(f":version\n")

    def load(self, fn):
        # WAVM mounts root, so it expects an absolute path
        if "wavm run" in self.exe:
            fn = "/" + fn

        self.loaded = None
        res = self._run_cmd(f":load {fn}\n")
        self.loaded = fn
        return res

    def invoke(self, cmd):
        return self._run_cmd(" ".join(map(str, cmd)) + "\n")

    def _run_cmd(self, cmd):
        if self.autorestart and not self._is_running():
            self.restart()
        self._flush_input()

        #print(f"wasm3: {cmd.strip()}")
        self._write(cmd)
        return self._read_until("wasm3> ")

    def _read_until(self, token):
        buff = ""
        tout = time.time() + self.timeout
        error = None

        while time.time() < tout:
            try:
                data = self.q.get(timeout=0.1)
                if data == None:
                    error = "Crashed"
                    break
                buff = buff + data.decode("utf-8")
                idx = buff.rfind(token)
                if idx >= 0:
                    return buff[0:idx].strip()
            except Empty:
                pass
        else:
            error = "Timeout"

        self.terminate()
        raise Exception(error)

    def _write(self, data):
        self.p.stdin.write(data.encode("utf-8"))
        self.p.stdin.flush()

    def _is_running(self):
        return self.p and (self.p.poll() == None)

    def _flush_input(self):
        while not self.q.empty():
            self.q.get()

    def terminate(self):
        self.p.stdin.close()
        self.p.terminate()
        self.p.wait(timeout=1.0)
        self.p = None

#
# Actual test
#

wasm3 = Wasm3(args.exec)

wasm3_ver = wasm3.version()
print(wasm3_ver)

blacklist = Blacklist([
  "float_exprs.wast:* f32.nonarithmetic_nan_bitpattern*",
  "imports.wast:*",
  "names.wast:* *.wasm \\x00*", # names that start with '\0'
])

if wasm3_ver in Blacklist(["* MSVC *, x86)", "* Clang * for Windows, x86)"]):
    warning("Win32 x86 has i64->f32 conversion precision issues, skipping some tests")
    # See: https://docs.microsoft.com/en-us/cpp/c-runtime-library/floating-point-support
    blacklist.add([
      "conversions.wast:* f32.convert_i64_u(9007199791611905)",
      "conversions.wast:* f32.convert_i64_u(9223371761976868863)",
      "conversions.wast:* f32.convert_i64_u(9223372586610589697)",
    ])

stats = dotdict(total_run=0, skipped=0, failed=0, crashed=0, timeout=0,  success=0, missing=0)

# Convert some trap names from the original spec
trapmap = {
  "unreachable": "unreachable executed"
}

def runInvoke(test):
    test.cmd = [test.action.field]

    displayArgs = []
    for arg in test.action.args:
        test.cmd.append(arg['value'])
        displayArgs.append(formatValue(arg['value'], arg['type']))

    test_id = f"{test.source} {test.wasm} {test.cmd[0]}({', '.join(test.cmd[1:])})"
    if test_id in blacklist and not args.all:
        warning(f"Skipped {test_id} (blacklisted)")
        stats.skipped += 1
        return

    if args.verbose:
        print(f"Running {test_id}")

    stats.total_run += 1

    output = ""
    actual = None
    actual_val = None
    force_fail = False

    try:
        output = wasm3.invoke(test.cmd)
    except Exception as e:
        actual = f"<{e}>"
        force_fail = True

    # Parse the actual output
    if not actual:
        result = re.findall(r'Result: (.*?)$', "\n" + output + "\n", re.MULTILINE)
        if len(result) > 0:
            actual = "result " + result[-1]
            actual_val = result[0]
    if not actual:
        result = re.findall(r'Error: \[trap\] (.*?) \(', "\n" + output + "\n", re.MULTILINE)
        if len(result) > 0:
            actual = "trap " + result[-1]
    if not actual:
        result = re.findall(r'Error: (.*?)$', "\n" + output + "\n", re.MULTILINE)
        if len(result) > 0:
            actual = "error " + result[-1]
    if not actual:
        actual = "<No Result>"
        force_fail = True

    if actual == "error no operation ()":
        actual = "<Not Implemented>"
        stats.missing += 1
        force_fail = True
    elif actual == "<Crashed>":
        stats.crashed += 1
        force_fail = True
    elif actual == "<Timeout>":
        stats.timeout += 1
        force_fail = True

    # Prepare the expected result
    expect = None
    if "expected" in test:
        if len(test.expected) == 0:
            expect = "result <Empty Stack>"
        elif len(test.expected) == 1:
            t = test.expected[0]['type']
            value = str(test.expected[0]['value'])
            expect = "result " + value

            if actual_val != None:
                if (t == "f32" or t == "f64") and (value == "nan:canonical" or value == "nan:arithmetic"):
                    val = binaryToFloat(actual_val, t)
                    #warning(f"{actual_val} => {val}")
                    if math.isnan(val):
                        actual = "nan:any"
                        expect = "nan:any"
                else:
                    expect = "result " + formatValue(value, t)
                    actual = "result " + formatValue(actual_val, t)

        else:
            warning(f"Test {test.source} specifies multiple results")
            expect = "result <Multiple>"
    elif "expected_trap" in test:
        if test.expected_trap in trapmap:
            test.expected_trap = trapmap[test.expected_trap]

        expect = "trap " + str(test.expected_trap)
    elif "expected_anything" in test:
        expect = "<Anything>"
    else:
        expect = "<Unknown>"

    def showTestResult():
        print(" ----------------------")
        print(f"Test:     {ansi.HEADER}{test_id}{ansi.ENDC}")
        print(f"Args:     {', '.join(displayArgs)}")
        print(f"Expected: {ansi.OKGREEN}{expect}{ansi.ENDC}")
        print(f"Actual:   {ansi.WARNING}{actual}{ansi.ENDC}")
        if args.show_logs and len(output):
            print(f"Log:")
            print(output)

    log.write(f"{test.source}\t|\t{test.wasm} {test.action.field}({', '.join(displayArgs)})\t=>\t\t")
    if actual == expect or (expect == "<Anything>" and not force_fail):
        stats.success += 1
        log.write(f"OK: {actual}\n")
        if args.line:
            showTestResult()
    else:
        stats.failed += 1
        log.write(f"FAIL: {actual}, should be: {expect}\n")
        if args.silent: return

        showTestResult()
        #sys.exit(1)

if args.file:
    jsonFiles = args.file
else:
    jsonFiles  = glob.glob(os.path.join(".", "core", "*.json"))
    jsonFiles += glob.glob(os.path.join(".", "proposals", "sign-extension-ops", "*.json"))
    jsonFiles += glob.glob(os.path.join(".", "proposals", "nontrapping-float-to-int-conversions", "*.json"))

jsonFiles = list(map(lambda x: os.path.relpath(x, scriptDir), jsonFiles))
jsonFiles.sort()

for fn in jsonFiles:
    with open(fn, encoding='utf-8') as f:
        data = json.load(f)

    wast_source = filename(data["source_filename"])
    wasm_module = ""

    print(f"Running {fn}")

    wasm3.init()

    for cmd in data["commands"]:
        test = dotdict()
        test.line = int(cmd["line"])
        test.source = wast_source + ":" + str(test.line)
        test.wasm = wasm_module
        test.type = cmd["type"]

        if test.type == "module":
            wasm_module = cmd["filename"]

            if args.verbose:
                print(f"Loading {wasm_module}")

            try:
                wasm_fn = os.path.join(pathname(fn), wasm_module)
                wasm3.load(wasm_fn)
            except Exception as e:
                pass #fatal(str(e))

        elif (  test.type == "action" or
                test.type == "assert_return" or
                test.type == "assert_trap" or
                test.type == "assert_exhaustion" or
                test.type == "assert_return_canonical_nan" or
                test.type == "assert_return_arithmetic_nan"):

            if args.line and test.line != args.line:
                continue

            if test.type == "action":
                test.expected_anything = True
            elif test.type == "assert_return":
                test.expected = cmd["expected"]
            elif test.type == "assert_return_canonical_nan":
                test.expected = cmd["expected"]
                test.expected[0]["value"] = "nan:canonical"
            elif test.type == "assert_return_arithmetic_nan":
                test.expected = cmd["expected"]
                test.expected[0]["value"] = "nan:arithmetic"
            elif test.type == "assert_trap":
                test.expected_trap = cmd["text"]
            elif test.type == "assert_exhaustion":
                test.expected_trap = "stack overflow"
            else:
                stats.skipped += 1
                warning(f"Skipped {test.source} ({test.type} not implemented)")
                continue

            test.action = dotdict(cmd["action"])
            if test.action.type == "invoke":

                # TODO: invoking in modules not implemented
                if test.action.module:
                    stats.skipped += 1
                    warning(f"Skipped {test.source} (invoke in module)")
                    continue

                test.action.field = escape_str(test.action.field)

                runInvoke(test)
            else:
                stats.skipped += 1
                warning(f"Skipped {test.source} (unknown action type '{test.action.type}')")


        # These are irrelevant
        elif (test.type == "assert_invalid" or
              test.type == "assert_malformed" or
              test.type == "assert_uninstantiable"):
            pass

        # Others - report as skipped
        else:
            stats.skipped += 1
            warning(f"Skipped {test.source} ('{test.type}' not implemented)")

if (stats.failed + stats.success) != stats.total_run:
    warning("Statistics summary invalid")

pprint(stats)

if stats.failed > 0:
    failed = (stats.failed*100)/stats.total_run
    print(f"{ansi.FAIL}=======================")
    print(f" FAILED: {failed:.2f}%")
    if stats.crashed > 0:
        print(f" Crashed: {stats.crashed}")
    print(f"======================={ansi.ENDC}")
    sys.exit(1)

elif stats.success > 0:
    print(f"{ansi.OKGREEN}=======================")
    print(f" {stats.success}/{stats.total_run} tests OK")
    if stats.skipped > 0:
        print(f"{ansi.WARNING} ({stats.skipped} tests skipped){ansi.OKGREEN}")
    print(f"======================={ansi.ENDC}")
