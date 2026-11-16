/*
 * AtomicCalculationDB tiny native bootstrapper.
 *
 * The .exe contains no Python runtime. It locates the PyInstaller onedir
 * runtime in .\runtime\ and forwards all arguments unchanged:
 *
 *   runtime\AtomicCalculationDB_Runtime.exe [--mcp | --mcp-sse ...]
 *
 * app_core/ and workspace/ stay external, so the in-app hot update remains
 * exactly the same while the shell itself is only a few tens of KiB.
 */
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <shellapi.h>
#include <stdlib.h>
#include <wchar.h>

#define APP_TITLE L"AtomicCalculationDB"

static void show_error(const wchar_t *message) {
    MessageBoxW(NULL, message, APP_TITLE L" - Startup Error",
                MB_OK | MB_ICONERROR);
}

static void dirname_w(wchar_t *path) {
    wchar_t *slash = wcsrchr(path, L'\\');
    wchar_t *forward = wcsrchr(path, L'/');
    if (forward && (!slash || forward > slash)) {
        slash = forward;
    }
    if (slash) {
        *slash = L'\0';
    }
}

static BOOL file_exists(const wchar_t *path) {
    DWORD attrs = GetFileAttributesW(path);
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

static void append_quoted(wchar_t *buffer, size_t capacity, size_t *length,
                          const wchar_t *arg) {
    if (*length >= capacity - 1) {
        return;
    }
    if (*length > 0) {
        buffer[(*length)++] = L' ';
    }

    BOOL quote = (arg[0] == L'\0');
    for (const wchar_t *p = arg; *p; ++p) {
        if (*p == L' ' || *p == L'\t' || *p == L'"') {
            quote = TRUE;
            break;
        }
    }
    if (!quote) {
        size_t n = wcslen(arg);
        if (*length + n < capacity) {
            wmemcpy(buffer + *length, arg, n);
            *length += n;
        }
        buffer[*length] = L'\0';
        return;
    }

    buffer[(*length)++] = L'"';
    size_t backslashes = 0;
    for (const wchar_t *p = arg; *p; ++p) {
        if (*p == L'\\') {
            backslashes++;
        } else if (*p == L'"') {
            for (size_t i = 0; i < backslashes * 2 + 1; ++i) {
                if (*length < capacity - 1) buffer[(*length)++] = L'\\';
            }
            if (*length < capacity - 1) buffer[(*length)++] = L'"';
            backslashes = 0;
        } else {
            for (size_t i = 0; i < backslashes; ++i) {
                if (*length < capacity - 1) buffer[(*length)++] = L'\\';
            }
            backslashes = 0;
            if (*length < capacity - 1) buffer[(*length)++] = *p;
        }
    }
    for (size_t i = 0; i < backslashes; ++i) {
        if (*length < capacity - 1) buffer[(*length)++] = L'\\';
    }
    if (*length < capacity - 1) buffer[(*length)++] = L'"';
    buffer[*length] = L'\0';
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command_line,
                    int show_command) {
    (void)instance;
    (void)previous;
    (void)command_line;
    (void)show_command;

    wchar_t exe_path[MAX_PATH * 2];
    DWORD length = GetModuleFileNameW(NULL, exe_path, (DWORD)(sizeof(exe_path) / sizeof(wchar_t)));
    if (length == 0 || length >= (DWORD)(sizeof(exe_path) / sizeof(wchar_t))) {
        show_error(L"Could not resolve the launcher path.");
        return 2;
    }
    wchar_t base_dir[MAX_PATH * 2];
    wcscpy_s(base_dir, sizeof(base_dir) / sizeof(wchar_t), exe_path);
    dirname_w(base_dir);

    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        show_error(L"Could not parse the command line.");
        return 2;
    }
    wchar_t runtime[MAX_PATH * 2];
    swprintf_s(runtime, sizeof(runtime) / sizeof(wchar_t),
               L"%ls\\runtime\\AtomicCalculationDB_Runtime.exe", base_dir);
    if (!file_exists(runtime)) {
        wchar_t message[2048];
        swprintf_s(message, sizeof(message) / sizeof(wchar_t),
                   L"The runtime is missing:\n%ls\n\n"
                   L"Please extract the complete release folder, not only the .exe.",
                   runtime);
        show_error(message);
        LocalFree(argv);
        return 2;
    }

    size_t command_capacity = 32768;
    wchar_t *command = (wchar_t *)calloc(command_capacity, sizeof(wchar_t));
    if (!command) {
        LocalFree(argv);
        return 3;
    }
    size_t cursor = 0;
    command[0] = L'\0';
    append_quoted(command, command_capacity, &cursor, runtime);
    for (int i = 1; i < argc; ++i) {
        append_quoted(command, command_capacity, &cursor, argv[i]);
    }

    SetEnvironmentVariableW(L"ACDB_SHELL_PATH", exe_path);
    SetEnvironmentVariableW(L"ACDB_RELEASE_ROOT", base_dir);
    SetEnvironmentVariableW(L"ACDB_PORTABLE_RUNTIME", L"1");
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");

    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    ZeroMemory(&startup, sizeof(startup));
    startup.cb = sizeof(startup);
    ZeroMemory(&process, sizeof(process));

    BOOL created = CreateProcessW(
        runtime,
        command,
        NULL,
        NULL,
        TRUE,                 /* inherit stdio pipes for --mcp */
        0,
        NULL,
        base_dir,
        &startup,
        &process);

    if (!created) {
        wchar_t message[1024];
        swprintf_s(message, sizeof(message) / sizeof(wchar_t),
                   L"Could not start the runtime.\nError code: %lu",
                   GetLastError());
        show_error(message);
        free(command);
        LocalFree(argv);
        return 3;
    }

    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD exit_code = 0;
    GetExitCodeProcess(process.hProcess, &exit_code);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    free(command);
    LocalFree(argv);
    return (int)exit_code;
}
