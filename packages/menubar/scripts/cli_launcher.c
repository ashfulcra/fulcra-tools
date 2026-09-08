/* Relocatable native CLI shim, signed with the rest of the application. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char executable[PATH_MAX], resolved[PATH_MAX], target[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) != 0 || !realpath(executable, resolved)) {
        fputs("Cannot locate the Collect application.\n", stderr);
        return 1;
    }
    char *name = strrchr(resolved, '/');
    if (!name) return 1;
    const char *flag = strcmp(name + 1, "fulcra-collect") == 0 ? "--collect" : "--fulcra";
    *name = '\0';
    int length = snprintf(target, sizeof(target), "%s/Fulcra Collect", resolved);
    if (length < 0 || length >= (int)sizeof(target)) return 1;
    char **args = calloc((size_t)argc + 2, sizeof(char *));
    if (!args) return 1;
    args[0] = target;
    args[1] = (char *)flag;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    execv(target, args);
    perror("Cannot start Fulcra Collect");
    free(args);
    return 1;
}
