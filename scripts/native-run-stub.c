#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/*
 * Tiny native CFBundleExecutable.
 *
 * Launch Services is more reliable when Contents/MacOS/run is a Mach-O binary.
 * The real app-it launcher stays in run.sh; this stub only locates run.sh next
 * to itself and execs it, preserving argv and environment.
 */
int main(int argc, char *argv[]) {
    uint32_t size = PATH_MAX;
    char stack_path[PATH_MAX];
    char *raw_path = stack_path;
    if (_NSGetExecutablePath(raw_path, &size) != 0) {
        raw_path = (char *)malloc(size);
        if (raw_path == NULL) {
            perror("malloc");
            return 127;
        }
        if (_NSGetExecutablePath(raw_path, &size) != 0) {
            fprintf(stderr, "app-it: could not resolve executable path\n");
            free(raw_path);
            return 127;
        }
    }

    char resolved[PATH_MAX];
    const char *exe_path = realpath(raw_path, resolved) ? resolved : raw_path;

    char *dir_input = strdup(exe_path);
    if (dir_input == NULL) {
        perror("strdup");
        if (raw_path != stack_path) {
            free(raw_path);
        }
        return 127;
    }

    char *dir = dirname(dir_input);
    size_t run_sh_len = strlen(dir) + strlen("/run.sh") + 1;
    char *run_sh = (char *)malloc(run_sh_len);
    if (run_sh == NULL) {
        perror("malloc");
        free(dir_input);
        if (raw_path != stack_path) {
            free(raw_path);
        }
        return 127;
    }
    snprintf(run_sh, run_sh_len, "%s/run.sh", dir);

    char **child_argv = (char **)calloc((size_t)argc + 1, sizeof(char *));
    if (child_argv == NULL) {
        perror("calloc");
        free(run_sh);
        free(dir_input);
        if (raw_path != stack_path) {
            free(raw_path);
        }
        return 127;
    }
    child_argv[0] = run_sh;
    for (int i = 1; i < argc; i++) {
        child_argv[i] = argv[i];
    }
    child_argv[argc] = NULL;

    execv(run_sh, child_argv);
    perror("execv run.sh");

    free(child_argv);
    free(run_sh);
    free(dir_input);
    if (raw_path != stack_path) {
        free(raw_path);
    }
    return 127;
}
