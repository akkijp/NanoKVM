// akkijp fork: tests for support/sg2002/kvm_system/main/lib/system_init/pending_record.h.
// Kept out of lib/system_init, whose every .cpp is compiled into kvm_system. Built and run by
// scripts/fork-test.sh:
//   g++ -std=c++11 -I support/sg2002/kvm_system/main/lib/system_init -o /tmp/t scripts/tests/pending_record_test.cpp
#include "pending_record.h"

#include <stdio.h>
#include <string.h>

int main()
{
	struct { const char *text; bool installed; } cases[] = {
		{"{\"from\":\"2.5.4\",\"to\":\"2.5.4.1\",\"phase\":\"installed\"}\n", true},         // Go
		{"{\"from\": \"2.5.4\", \"to\": \"2.5.4.1\", \"phase\": \"installed\"}\n", true},    // Python
		{"{\"from\":\"\",\"to\":\"2.5.4.1\",\"phase\":\"installed\"}", true},               // no previous version
		{"{\"from\":\"2.5.4\",\"to\":,\"phase\":\"installed\"}\n", false},                   // broken JSON
		{"{\"from\":\"2.5.4\",\"to\":\"\",\"phase\":\"installed\"}\n", false},               // no target
		{"{\"from\":\"2.5.4\",\"to\":\"2.5.4.1\",\"phase\":\"switching\"}\n", false},
		{"{\"from\":\"2.5.4\",\"to\":\"2.5.4.1\",\"phase\":\"installed\"", false},           // truncated
		{"{\"from\":\"2.5.4\",\"to\":\"2.5.4.1\",\"phase\":\"installed\"}\n{\"x\":1}", false}, // extra line
		{"{\"from\":\"2.5.4\",\"to\":\"2.5.4.1\",\"phase\":\"installed\"}\n\n", false},      // extra newline
		{"{\"to\":\"2.5.4.1\",\"from\":\"2.5.4\",\"phase\":\"installed\"}", false},          // other shape
		{"{\"from\":\"a\\\"b\",\"to\":\"2.5.4.1\",\"phase\":\"installed\"}", false},         // escapes
		{"", false},
	};
	int failures = 0;
	for(const auto &c : cases) {
		if(pending_record::installed(c.text, strlen(c.text)) != c.installed) {
			printf("wrong for: %s\n", c.text);
			failures++;
		}
	}
	if(failures == 0) printf("pending_record: all %zu cases pass\n", sizeof(cases) / sizeof(cases[0]));
	return failures == 0 ? 0 : 1;
}
