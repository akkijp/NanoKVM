package application

import (
	"archive/tar"
	"compress/gzip"
	"os"
	"path/filepath"
	"testing"
)

type archiveEntry struct {
	name     string
	typeFlag byte
	data     string
}

func writeTestArchive(t *testing.T, entries []archiveEntry) string {
	t.Helper()
	file := filepath.Join(t.TempDir(), "update.tar.gz")
	out, err := os.Create(file)
	if err != nil {
		t.Fatal(err)
	}
	gzipWriter := gzip.NewWriter(out)
	tarWriter := tar.NewWriter(gzipWriter)
	for _, entry := range entries {
		header := &tar.Header{Name: entry.name, Typeflag: entry.typeFlag, Mode: 0o755}
		if entry.typeFlag == tar.TypeReg || entry.typeFlag == tar.TypeRegA {
			header.Size = int64(len(entry.data))
		}
		if err := tarWriter.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if entry.data != "" {
			if _, err := tarWriter.Write([]byte(entry.data)); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := tarWriter.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gzipWriter.Close(); err != nil {
		t.Fatal(err)
	}
	if err := out.Close(); err != nil {
		t.Fatal(err)
	}
	return file
}

func TestInspectArchiveRejectsUnsafeEntries(t *testing.T) {
	root := "nanokvm_1.2.3"
	for _, entry := range []archiveEntry{
		{name: "../escape", typeFlag: tar.TypeReg, data: "x"},
		{name: "/escape", typeFlag: tar.TypeReg, data: "x"},
		{name: root + "/link", typeFlag: tar.TypeSymlink},
	} {
		archive := writeTestArchive(t, []archiveEntry{{name: root, typeFlag: tar.TypeDir}, entry})
		if _, err := inspectUpdateArchive(archive, root); err == nil {
			t.Fatalf("unsafe entry %+v was accepted", entry)
		}
	}
}

func TestInspectAndExtractArchive(t *testing.T) {
	root := "nanokvm_1.2.3"
	entries := packageEntries(root, "1.2.3", "")
	archive := writeTestArchive(t, entries)
	info, err := inspectUpdateArchive(archive, root)
	if err != nil {
		t.Fatal(err)
	}
	var want uint64
	for _, entry := range entries {
		want += uint64(len(entry.data))
	}
	if info.expandedBytes != want {
		t.Fatalf("unexpected expanded size %d, want %d", info.expandedBytes, want)
	}
	destination := t.TempDir()
	extracted, err := extractUpdateArchive(archive, destination, root)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateExtractedPackage(extracted, "1.2.3"); err != nil {
		t.Fatal(err)
	}
}

// packageEntries is a minimal valid package: every required file, version saying inner
// (or version itself), and none of the files listed in skip.
func packageEntries(root, version, skip string) []archiveEntry {
	entries := []archiveEntry{{name: root, typeFlag: tar.TypeDir}}
	for _, file := range requiredPackageFiles {
		if file == skip {
			continue
		}
		data := "x"
		if file == "version" {
			data = version + "\n"
		}
		entries = append(entries, archiveEntry{name: root + "/" + file, typeFlag: tar.TypeReg, data: data})
	}
	return entries
}
