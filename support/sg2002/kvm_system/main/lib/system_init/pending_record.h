// akkijp fork: the record of an update not yet seen running (/etc/kvm/update-restart-pending,
// written by server/service/application/pending.go and kvmapp/system/update-nanokvm.py).
//
// Self-contained (no SDK headers) so scripts/tests can compile and test it on its own.
#pragma once

#include <string.h>

namespace pending_record {

// Exactly one complete record that says the update is installed:
//   {"from":"<text>","to":"<non-empty text>","phase":"installed"}
// with at most spaces after ':' and ',', and one optional trailing newline. Strings must not
// contain quotes or backslashes (versions never do). Anything else, including a record that is
// valid JSON but in another shape, counts as not installed.
inline bool installed(const char *text, size_t length)
{
	size_t i = 0;
	auto skip_spaces = [&]() { while(i < length && text[i] == ' ') i++; };
	auto literal = [&](const char *word) {
		size_t n = strlen(word);
		if(length - i < n || strncmp(text + i, word, n) != 0) return false;
		i += n;
		return true;
	};
	auto string_value = [&](bool non_empty) {
		if(i >= length || text[i] != '"') return false;
		size_t start = ++i;
		while(i < length && text[i] != '"') {
			if(text[i] == '\\' || text[i] == '\n' || text[i] == '\0') return false;
			i++;
		}
		if(i >= length) return false;
		bool ok = !non_empty || i > start;
		i++;  // closing quote
		return ok;
	};
	auto field = [&](const char *key, bool non_empty) {
		if(!literal(key)) return false;
		skip_spaces();
		return string_value(non_empty);
	};

	if(!literal("{")) return false;
	if(!field("\"from\":", false)) return false;
	if(!literal(",")) return false;
	skip_spaces();
	if(!field("\"to\":", true)) return false;
	if(!literal(",")) return false;
	skip_spaces();
	if(!literal("\"phase\":")) return false;
	skip_spaces();
	if(!literal("\"installed\"")) return false;
	if(!literal("}")) return false;
	if(i < length && text[i] == '\n') i++;
	return i == length;
}

}  // namespace pending_record
