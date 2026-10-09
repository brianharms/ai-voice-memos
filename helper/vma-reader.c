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
 *
 * Its Full Disk Access grant would let it read anything, so it refuses any
 * <dir>/<src> whose canonical path (symlinks and .. resolved) is not inside
 * ~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings
 * (home taken from the password database, not $HOME). Exit 3 on refusal.
 */
#include <stdio.h>
#include <string.h>
#include <dirent.h>
#include <sys/stat.h>
#include <stdlib.h>
#include <limits.h>
#include <unistd.h>
#include <pwd.h>

/* 1 if `path` canonicalizes to the Recordings container or a path inside it */
static int allowed(const char *path) {
    struct passwd *pw = getpwuid(getuid());
    if (!pw || !pw->pw_dir) return 0;
    char root_raw[PATH_MAX], root[PATH_MAX], real[PATH_MAX];
    snprintf(root_raw, sizeof root_raw,
             "%s/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings", pw->pw_dir);
    if (!realpath(root_raw, root)) return 0;
    if (!realpath(path, real)) return 0;
    size_t n = strlen(root);
    return strncmp(real, root, n) == 0 && (real[n] == '\0' || real[n] == '/');
}

static int cmd_list(const char *dir) {
    if (!allowed(dir)) { fprintf(stderr, "refused: %s is outside the Voice Memos recordings folder\n", dir); return 3; }
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
    if (!allowed(src)) { fprintf(stderr, "refused: %s is outside the Voice Memos recordings folder\n", src); return 3; }
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
