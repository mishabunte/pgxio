use strict;
use warnings;
# Both pinned upstream versions use wall time for request and event-loop timers.
# Wall-clock corrections must not affect elapsed time or histogram indexes.
for my $path (@ARGV) {
    open my $in, '<', $path or die "$path: $!";
    local $/;
    my $source = <$in>;
    close $in;
    if ($path =~ m{/wrk\.c$}) {
        my $count = ($source =~ s{static uint64_t time_us\(\)\s*\{.*?^\}}{static uint64_t time_us() {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) abort();
    return (uint64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000;
}}ms);
        die "unexpected request clock in $path" unless $count == 1;
    } elsif ($path =~ m{/ae\.c$}) {
        my $count = ($source =~ s{static void aeGetTime\(long \*seconds, long \*milliseconds\)\s*\{.*?^\}}{static void aeGetTime(long *seconds, long *milliseconds) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) abort();
    *seconds = t.tv_sec;
    *milliseconds = t.tv_nsec / 1000000;
}}ms);
        die "unexpected event clock in $path" unless $count == 1;
        $source =~ s/time\(NULL\)/aeMonotonicSeconds()/g;
        $source = "#include <time.h>\n#include <stdlib.h>\nstatic time_t aeMonotonicSeconds(void) {\n    struct timespec t;\n    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) abort();\n    return t.tv_sec;\n}\n" . $source;
    } else { die "unexpected file $path"; }
    $source = "#include <time.h>\n#include <stdlib.h>\n" . $source;
    die "wall clock remains in $path" if $source =~ /gettimeofday|time\(NULL\)/;
    open my $out, '>', $path or die "$path: $!";
    print {$out} $source;
    close $out;
}
