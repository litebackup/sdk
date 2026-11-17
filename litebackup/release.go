package litebackup

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
)

const (
	// signatureContext and signatureSeparator frame signed release metadata:
	// context, key ID, separator, document.
	signatureContext   = "clor-apps-v1\x00"
	signatureSeparator = "\x00"
	// CommandPackage is the package that ships the litebackup command.
	CommandPackage = "cli"
	// StableChannel is the channel installs follow unless a version is given.
	StableChannel = "stable"
)

// ReleaseKeys are Litebackup's Ed25519 release keys by key ID, base64url
// without padding, as the litebackup command checks them.
var ReleaseKeys = map[string]string{
	"01a1ead6-4b94-79ca-81f4-3a67c112bc0b": "t3C_3l71PimyhkPZOzJ_Xi4dOuHJ1CmSdDZ8lTm0vNI",
}

// ErrSignature means a release's signature does not match ReleaseKeys.
var ErrSignature = errors.New("the release signature does not match Litebackup's release keys")

// ReleaseFile is one published file of a release.
type ReleaseFile struct {
	Name         string `json:"name"`
	Filename     string `json:"filename"`
	URL          string `json:"url"`
	SHA256       string `json:"sha256"`
	Size         int64  `json:"size"`
	OS           string `json:"os,omitempty"`
	Architecture string `json:"architecture,omitempty"`
}

// Release is a package release taken from its verified signed document.
type Release struct {
	Version  string        `json:"version"`
	Channels []string      `json:"-"`
	Files    []ReleaseFile `json:"artifacts"`
}

// File returns the release file with the given name.
func (r *Release) File(name string) (*ReleaseFile, error) {
	for index := range r.Files {
		if r.Files[index].Name == name {
			return &r.Files[index], nil
		}
	}
	return nil, fmt.Errorf("release %s has no file %s", r.Version, name)
}

// signedDocument is a document with its Ed25519 signature.
type signedDocument struct {
	KeyID     string `json:"key_id"`
	Document  string `json:"document"`
	Signature string `json:"signature"`
}

// packageMetadata is the website's package listing, /pkg/metadata.
type packageMetadata struct {
	Packages []struct {
		Slug     string `json:"slug"`
		Releases []struct {
			Version   string         `json:"version"`
			Channels  []string       `json:"channels"`
			Signature signedDocument `json:"signature"`
		} `json:"releases"`
	} `json:"packages"`
}

// FindReleaseOptions selects a release from package metadata.
type FindReleaseOptions struct {
	// Package is the package slug; CommandPackage when empty.
	Package string
	// Version is the release to find; the release on Channel when empty.
	Version string
	// Channel is used when Version is empty; StableChannel when empty.
	Channel string
	// Keys verify the release; ReleaseKeys when nil.
	Keys map[string]string
}

// FindRelease reads package metadata, as https://litebackup.com/pkg/metadata
// serves it, and returns the selected release taken from its signed
// document, never the unsigned listing. A release whose signature does not
// verify returns ErrSignature.
func FindRelease(metadata []byte, options FindReleaseOptions) (*Release, error) {
	slug := options.Package
	if slug == "" {
		slug = CommandPackage
	}
	channel := options.Channel
	if channel == "" {
		channel = StableChannel
	}
	keys := options.Keys
	if keys == nil {
		keys = ReleaseKeys
	}
	var listing packageMetadata
	if err := json.Unmarshal(metadata, &listing); err != nil {
		return nil, fmt.Errorf("package metadata: %w", err)
	}
	for _, listed := range listing.Packages {
		if listed.Slug != slug {
			continue
		}
		for _, candidate := range listed.Releases {
			matches := candidate.Version == options.Version
			if options.Version == "" {
				matches = slices.Contains(candidate.Channels, channel)
			}
			if !matches {
				continue
			}
			release, err := verifyRelease(&candidate.Signature, keys)
			if err != nil {
				return nil, err
			}
			if release.Version != candidate.Version {
				return nil, ErrSignature
			}
			release.Channels = candidate.Channels
			return release, nil
		}
		if options.Version != "" {
			return nil, fmt.Errorf("package %s has no release %s", slug, options.Version)
		}
		return nil, fmt.Errorf("package %s has no release on %s", slug, channel)
	}
	return nil, fmt.Errorf("no package %s", slug)
}

// verifyRelease checks a signed release against keys and decodes it.
func verifyRelease(signed *signedDocument, keys map[string]string) (*Release, error) {
	encoded, found := keys[signed.KeyID]
	if !found {
		return nil, ErrSignature
	}
	key, err := base64.RawURLEncoding.DecodeString(encoded)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return nil, ErrSignature
	}
	document, err := base64.RawURLEncoding.DecodeString(signed.Document)
	if err != nil {
		return nil, ErrSignature
	}
	signature, err := base64.RawURLEncoding.DecodeString(signed.Signature)
	if err != nil {
		return nil, ErrSignature
	}
	message := append([]byte(signatureContext+signed.KeyID+signatureSeparator), document...)
	if !ed25519.Verify(ed25519.PublicKey(key), message, signature) {
		return nil, ErrSignature
	}
	var release Release
	if err := json.Unmarshal(document, &release); err != nil {
		return nil, ErrSignature
	}
	return &release, nil
}
