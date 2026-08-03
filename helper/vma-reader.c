/*
 * vma-reader — the ONLY code in Voice Memo AI that touches the TCC-protected
 * Voice Memos container.
 *
 * Why this exists: macOS ties privacy grants (Full Disk Access) to a binary's
 * code identity. Python interpreters are moving targets — brew upgrades,
 * Xcode updates, and shim resolution all change the identity and silently kill
 * the grant (observed live, repeatedly). This binary is ~50 lines, built once,
 * installed at a stable path (~/.voice-memo-ai/bin/vma-reader), and never
 * rebuilt unless deliberately changed — so ONE Full Disk Access grant to it
 * holds forever. The watcher (python, no grants needed) shells out to it.
 *
 * Commands:
 *   vma-reader list <dir>          ->  "<size>\t<mtime>\t<name>\n" per regular file
 *   vma-reader copy <src> <dst>    ->  byte copy (exit 0 on success)
 */
#include <stdio.h>
#include <string.h>
#include <dirent.h>
#include <sys/stat.h>

static int cmd_list(const char *dir) {
    DIR *d = opendir(dir);
    if (!d) { perror("opendir"); return 1; }
    struct dirent *e;
    char path[4096];
    struct stat st;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        snprintf(path, sizeof path, "%s/%s", dir, e->d_name);
        if (stat(path, &st) != 0 || !S_ISREG(st.st_mode)) continue;
        printf("%lld\t%.6f\t%s\n",
               (long long)st.st_size,
               (double)st.st_mtimespec.tv_sec + (double)st.st_mtimespec.tv_nsec / 1e9,
               e->d_name);
    }
    closedir(d);
    return 0;
}

static int cmd_copy(const char *src, const char *dst) {
    FILE *in = fopen(src, "rb");
    if (!in) { perror("open src"); return 1; }
    FILE *out = fopen(dst, "wb");
    if (!out) { perror("open dst"); fclose(in); return 1; }
    char buf[1 << 16];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, in)) > 0) {
        if (fwrite(buf, 1, n, out) != n) { perror("write"); fclose(in); fclose(out); return 1; }
    }
    int rerr = ferror(in);
    fclose(in);
    if (fclose(out) != 0 || rerr) { perror("close"); return 1; }
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 3 && strcmp(argv[1], "list") == 0) return cmd_list(argv[2]);
    if (argc == 4 && strcmp(argv[1], "copy") == 0) return cmd_copy(argv[2], argv[3]);
    fprintf(stderr, "usage: vma-reader list <dir> | copy <src> <dst>\n");
    return 2;
}
