/*
 * agent-scan — find tmux panes running AI coding agents.
 *
 * Output, one row per agent pane, tab-separated, grouped by project
 * directory in stable pane order:
 *   busy  pane_id  session_id:window_id  display-line  search-text  title
 * Fields 1-3 are consumed by scripts; field 4 is the formatted row shown
 * in the picker (project, state, agent, title).
 *
 * Detection: tmux tells us each pane's shell pid; the kernel tells us every
 * process's parent. Invert the parent pointers into a tree, walk down from
 * each pane's shell, and match command lines against the agent list. We
 * only fetch argv for processes that live under a pane (a dozen or so),
 * never for the whole system — that per-process argv fetch is exactly what
 * made `ps -Ao args=` slow.
 *
 * Build: cc -O2 -o agent-scan agent-scan.c
 */
#define _GNU_SOURCE /* strcasestr, strsep on glibc */
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef __APPLE__
#include <sys/sysctl.h>
#include <sys/types.h>
#else
#include <dirent.h>
#endif

#define MAX_PANES 128
#define MAX_PROCS 8192
#define MAX_AGENTS 32
#define TAIL_LINES 20 /* pane-bottom window searched for the busy marker */

typedef struct {
    char id[16];       /* tmux pane id, e.g. "%42" */
    char target[128];  /* "session:window_index" */
    int pid;           /* the pane's shell */
    char path[512];
    char title[256];
    const char *agent; /* NULL = no agent in this pane */
    int busy;
    char context[256];
    char window[256];
    char label[256];
} Pane;

static struct { int pid, ppid; } procs[MAX_PROCS];
static int nprocs;

static char *agents[MAX_AGENTS];
static int nagents;

/* ------------------------------------------------------------------ panes */

static void clean_text(char *s)
{
    for (; *s; s++)
        if ((unsigned char)*s < 32 || (unsigned char)*s == 127) *s = ' ';
}

static int read_panes(Pane *panes)
{
    FILE *fp = popen("exec tmux list-panes -a -F "
                     "'#{pane_id}\t#{session_id}\t#{window_id}\t"
                     "#{pane_pid}\t#{pane_current_path}\t#{session_name}:#{window_index}\t#{window_name}\t#{@agent-harpoon-label}\t#{pane_title}'",
                     "r");
    if (!fp)
        return 0;

    char line[2048];
    int n = 0;
    while (n < MAX_PANES && fgets(line, sizeof line, fp)) {
        line[strcspn(line, "\n")] = '\0';
        char *rest = line;
        char *id = strsep(&rest, "\t");
        char *sess = strsep(&rest, "\t");
        char *widx = strsep(&rest, "\t");
        char *pid = strsep(&rest, "\t");
        char *path = strsep(&rest, "\t");
        char *context = strsep(&rest, "\t");
        char *window = strsep(&rest, "\t");
        char *label = strsep(&rest, "\t");
        char *title = rest; /* remainder — may itself contain tabs */
        if (!path)
            continue;
        Pane *p = &panes[n++];
        snprintf(p->id, sizeof p->id, "%s", id);
        snprintf(p->target, sizeof p->target, "%s:%s", sess, widx);
        snprintf(p->path, sizeof p->path, "%s", path);
        snprintf(p->title, sizeof p->title, "%s", title ? title : "");
        snprintf(p->context, sizeof p->context, "%s", context ? context : "");
        snprintf(p->window, sizeof p->window, "%s", window ? window : "");
        snprintf(p->label, sizeof p->label, "%s", label ? label : "");
        clean_text(p->window);
        clean_text(p->label);
        clean_text(p->title);
        clean_text(p->path);
        clean_text(p->context);
        p->pid = atoi(pid);
        p->agent = NULL;
        p->busy = -1;
    }
    pclose(fp);
    return n;
}

/* -------------------------------------------------------- process table */

#ifdef __APPLE__

/* One sysctl gives us every process's (pid, ppid) as structs — the same
 * data `ps` prints, minus the fork/exec and text round trip. */
static void read_procs(void)
{
    int mib[4] = {CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0};
    size_t len = 0;
    if (sysctl(mib, 4, NULL, &len, NULL, 0) < 0)
        return;
    len += len / 4; /* headroom for processes spawned since the size query */
    struct kinfo_proc *kp = malloc(len);
    if (!kp)
        return;
    if (sysctl(mib, 4, kp, &len, NULL, 0) < 0) {
        free(kp);
        return;
    }
    int n = (int)(len / sizeof *kp);
    for (int i = 0; i < n && nprocs < MAX_PROCS; i++) {
        procs[nprocs].pid = kp[i].kp_proc.p_pid;
        procs[nprocs].ppid = kp[i].kp_eproc.e_ppid;
        nprocs++;
    }
    free(kp);
}

/* argv of one process joined with tabs, or NULL if unreadable (zombie,
 * exited, not ours). KERN_PROCARGS2 layout: int argc, then the exec path,
 * NUL padding, then the argc argv strings NUL-separated, then environ. */
static char *proc_args(int pid)
{
    static int argmax;
    if (!argmax) {
        int mib[2] = {CTL_KERN, KERN_ARGMAX};
        size_t sz = sizeof argmax;
        if (sysctl(mib, 2, &argmax, &sz, NULL, 0) < 0)
            argmax = 262144;
    }
    char *buf = malloc((size_t)argmax);
    if (!buf)
        return NULL;
    int mib[3] = {CTL_KERN, KERN_PROCARGS2, pid};
    size_t size = (size_t)argmax;
    if (sysctl(mib, 3, buf, &size, NULL, 0) < 0) {
        free(buf);
        return NULL;
    }

    int argc;
    memcpy(&argc, buf, sizeof argc);
    char *p = buf + sizeof argc, *end = buf + size;
    while (p < end && *p)
        p++; /* skip exec path */
    while (p < end && !*p)
        p++; /* skip NUL padding */

    char *out = malloc(size + 1);
    if (!out) {
        free(buf);
        return NULL;
    }
    size_t o = 0;
    for (int a = 0; a < argc && p < end; a++) {
        if (a)
            out[o++] = '\t';
        while (p < end && *p)
            out[o++] = *p++;
        p++;
    }
    out[o] = '\0';
    free(buf);
    return out;
}

#else /* Linux */

static void read_procs(void)
{
    DIR *d = opendir("/proc");
    if (!d)
        return;
    struct dirent *e;
    while ((e = readdir(d)) && nprocs < MAX_PROCS) {
        if (!isdigit((unsigned char)e->d_name[0]))
            continue;
        char sp[64];
        snprintf(sp, sizeof sp, "/proc/%s/stat", e->d_name);
        FILE *f = fopen(sp, "r");
        if (!f)
            continue;
        char buf[512];
        char *ok = fgets(buf, sizeof buf, f);
        fclose(f);
        if (!ok)
            continue;
        /* "pid (comm) state ppid ..." — comm may contain spaces/parens,
         * so parse from the last ')' */
        char *rp = strrchr(buf, ')');
        int ppid;
        if (!rp || sscanf(rp + 1, " %*c %d", &ppid) != 1)
            continue;
        procs[nprocs].pid = atoi(e->d_name);
        procs[nprocs].ppid = ppid;
        nprocs++;
    }
    closedir(d);
}

static char *proc_args(int pid)
{
    char cp[64];
    snprintf(cp, sizeof cp, "/proc/%d/cmdline", pid);
    FILE *f = fopen(cp, "r");
    if (!f)
        return NULL;
    char *buf = malloc(65536);
    size_t n = buf ? fread(buf, 1, 65535, f) : 0;
    fclose(f);
    if (n == 0) {
        free(buf);
        return NULL;
    }
    for (size_t i = 0; i + 1 < n; i++) /* NUL separators -> tabs */
        if (!buf[i])
            buf[i] = '\t';
    buf[n] = '\0';
    return buf;
}

#endif

/* ------------------------------------------------------------- matching */

/* "2.1.217" — claude's CLI retitles its process to a bare version number */
static int looks_like_version(const char *s)
{
    int dots = 0;
    if (!isdigit((unsigned char)*s))
        return 0;
    for (; *s; s++) {
        if (*s == '.') {
            if (!isdigit((unsigned char)s[1]))
                return 0;
            dots++;
        } else if (!isdigit((unsigned char)*s)) {
            return 0;
        }
    }
    return dots == 2;
}

static const char *match_agent(const char *args)
{
    /* basename of argv[0] */
    char word[256];
    size_t i = 0;
    while (args[i] && args[i] != '\t' && i < sizeof word - 1) {
        word[i] = args[i];
        i++;
    }
    word[i] = '\0';
    char *slash = strrchr(word, '/');
    const char *base = slash ? slash + 1 : word;

    for (int a = 0; a < nagents; a++)
        if (strcmp(base, agents[a]) == 0)
            return agents[a];
    if (looks_like_version(base))
        return "claude"; /* retitled CLI */
    /* JS launchers have node as argv[0]. Match the script path, never arbitrary
     * prompt arguments. Pi's source checkout also uses --import tsx/loader.mjs. */
    if (strcmp(base, "node") == 0 || strcmp(base, "bun") == 0) {
        char *copy = strdup(args), *rest = copy;
        if (!copy) return NULL;
        strsep(&rest, "\t");
        char *arg;
        while ((arg = strsep(&rest, "\t"))) {
            if (strcmp(arg, "--import") == 0 || strcmp(arg, "--loader") == 0 ||
                strcmp(arg, "--require") == 0 || strcmp(arg, "-r") == 0) {
                strsep(&rest, "\t");
                continue;
            }
            if (*arg == '-') continue;
            const char *found = NULL;
            if (strstr(arg, "/pi-coding-agent/") ||
                strstr(arg, "/packages/coding-agent/src/cli.ts") ||
                strstr(arg, "/packages/coding-agent/dist/cli.js")) found = "pi";
            else if (strstr(arg, "/@anthropic-ai/claude-code/")) found = "claude";
            else if (strstr(arg, "/@openai/codex/")) found = "codex";
            free(copy);
            return found;
        }
        free(copy);
    }
    return NULL;
}

/* BFS down from the pane's shell until something agent-shaped turns up */
static const char *find_agent(int root)
{
    int queue[256], qn = 0;
    queue[qn++] = root;
    for (int qi = 0; qi < qn; qi++) {
        char *args = proc_args(queue[qi]);
        if (args) {
            const char *agent = match_agent(args);
            free(args);
            if (agent)
                return agent;
        }
        for (int i = 0; i < nprocs && qn < 256; i++)
            if (procs[i].ppid == queue[qi])
                queue[qn++] = procs[i].pid;
    }
    return NULL;
}

/* ----------------------------------------------------------- busy state */

static Pane *find_pane(Pane *panes, int npanes, const char *id)
{
    for (int i = 0; i < npanes; i++)
        if (strcmp(panes[i].id, id) == 0)
            return &panes[i];
    return NULL;
}

static int tail_has_marker(char ring[][512], int nlines)
{
    int n = nlines < TAIL_LINES ? nlines : TAIL_LINES;
    for (int i = 0; i < n; i++)
        if (strcasestr(ring[i], "esc to interrupt"))
            return 1;
    return -1;
}

/* One tmux invocation captures every agent pane's screen, each prefixed by
 * a marker line. "esc to interrupt" in the last TAIL_LINES lines means the
 * agent is mid-task; otherwise activity is unknown. This is a best-effort screen heuristic. */
static void mark_busy(Pane *panes, int npanes)
{
    char cmd[16384];
    size_t off = (size_t)snprintf(cmd, sizeof cmd, "exec tmux");
    int any = 0;
    for (int i = 0; i < npanes; i++) {
        if (!panes[i].agent)
            continue;
        /* display-message expands strftime-style %-codes, so the pane id's
         * leading '%' must be doubled or "%1" silently becomes "1" */
        off += (size_t)snprintf(cmd + off, sizeof cmd - off,
                                "%s display-message -p '@@AP:%%%%%s' \\; "
                                "capture-pane -p -t '%s'",
                                any ? " \\;" : "", panes[i].id + 1,
                                panes[i].id);
        any = 1;
        if (off > sizeof cmd - 256)
            break;
    }
    if (!any)
        return;

    FILE *fp = popen(cmd, "r");
    if (!fp)
        return;

    char ring[TAIL_LINES][512];
    int nlines = 0;
    Pane *cur = NULL;
    char line[4096];

    while (fgets(line, sizeof line, fp)) {
        line[strcspn(line, "\n")] = '\0';
        if (strncmp(line, "@@AP:", 5) == 0) {
            if (cur)
                cur->busy = tail_has_marker(ring, nlines);
            cur = find_pane(panes, npanes, line + 5);
            nlines = 0;
            continue;
        }
        snprintf(ring[nlines % TAIL_LINES], sizeof ring[0], "%s", line);
        nlines++;
    }
    if (cur)
        cur->busy = tail_has_marker(ring, nlines);
    pclose(fp);
}

/* --------------------------------------------------------------- output */

static int cmp_pane(const void *a, const void *b)
{
    const Pane *pa = *(Pane *const *)a, *pb = *(Pane *const *)b;
    int c = strcmp(pa->path, pb->path); /* group by project directory */
    if (c)
        return c;
    return atoi(pa->id + 1) - atoi(pb->id + 1); /* do not reorder on activity */
}

#define FOLDER_MAX 24 /* codepoints of project name before truncating */

/* display width ~= codepoint count; exact for our glyphs (no CJK/emoji) */
static int utf8_width(const char *s)
{
    int w = 0;
    for (; *s; s++)
        if (((unsigned char)*s & 0xC0) != 0x80)
            w++;
    return w;
}

/* print s truncated to max codepoints (ellipsis if cut), padded to width */
static void print_col(const char *s, int max, int width)
{
    int truncated = utf8_width(s) > max;
    int limit = truncated ? max - 1 : max;
    int w = 0;
    while (*s && w < limit) {
        putchar(*s++);
        while (((unsigned char)*s & 0xC0) == 0x80)
            putchar(*s++); /* continuation bytes of a multibyte char */
        w++;
    }
    if (truncated) {
        fputs("…", stdout);
        w++;
    }
    while (w++ < width)
        putchar(' ');
}

static void init_agents(void)
{
    static char list[512];
    const char *env = getenv("AGENT_PICKER_AGENTS");
    snprintf(list, sizeof list, "%s",
             (env && *env) ? env
                           : "claude|codex|opencode|aider|pi|goose|amp|gemini");
    char *save = list, *tok;
    while (nagents < MAX_AGENTS && (tok = strsep(&save, "|")))
        if (*tok)
            agents[nagents++] = tok;
}

int main(void)
{
    init_agents();

    static Pane panes[MAX_PANES];
    int npanes = read_panes(panes);
    if (npanes == 0)
        return 0;

    read_procs();

    Pane *hits[MAX_PANES];
    int nhits = 0;
    for (int i = 0; i < npanes; i++) {
        panes[i].agent = find_agent(panes[i].pid);
        if (panes[i].agent)
            hits[nhits++] = &panes[i];
    }
    if (nhits == 0)
        return 0;

    mark_busy(panes, npanes);
    qsort(hits, nhits, sizeof *hits, cmp_pane);

    const char *folders[MAX_PANES], *titles[MAX_PANES];
    char host[256] = "";
    gethostname(host, sizeof host - 1);
    int wtitle = 0;
    for (int i = 0; i < nhits; i++) {
        Pane *p = hits[i];
        const char *slash = strrchr(p->path, '/');
        folders[i] = (slash && slash[1]) ? slash + 1 : p->path;
        const char *title = p->label;
        if (!*title && *p->title && strcmp(p->title, host) != 0 &&
            strcmp(p->title, folders[i]) != 0 && strcmp(p->title, p->path) != 0 &&
            strcmp(p->title, p->agent) != 0)
            title = p->title;
        if (!*title && *p->window && strcmp(p->window, p->agent) != 0 &&
            strcmp(p->window, "bash") != 0 && strcmp(p->window, "zsh") != 0 &&
            strcmp(p->window, "fish") != 0)
            title = p->window;
        titles[i] = *title ? title : folders[i];
        int w = utf8_width(titles[i]);
        if (w > 32) w = 32;
        if (w > wtitle) wtitle = w;
    }

    /* Fields 1-3 route actions. 4 is compact display, 5 is complete searchable
     * metadata, 6 is the untruncated title used for naming and grep results. */
    for (int i = 0; i < nhits; i++) {
        Pane *p = hits[i];
        printf("%d\t%s\t%s\t", p->busy, p->id, p->target);
        print_col(titles[i], 32, wtitle);
        printf("  %s ", p->busy == 1 ? "●" : " ");
        fputs("\033[2m", stdout);
        if (strcmp(titles[i], folders[i]) != 0)
            printf("%s · ", folders[i]);
        printf("%s · %s\033[0m", p->agent, p->context);
        printf("\t%s %s %s %s %s %s\t%s\n", titles[i], p->path,
               p->agent, p->title, p->window, p->context, titles[i]);
    }
    return 0;
}
