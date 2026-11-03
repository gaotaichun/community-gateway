// Command mockdiscord is a tiny stand-in for the Discord HTTP API used by the
// end-to-end tests. It is NOT part of the shipped gateway; it only exists so the
// OAuth + guild gate can be exercised without touching a real Discord server.
//
// Usage:
//
//	go run ./tools/mockdiscord -addr 127.0.0.1:4010 -config ./mock.json
//
// The config file is re-read on every request, so a test can switch scenarios
// (member / non-member / missing role) by rewriting the file.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
)

type MockGuild struct {
	Roles []string `json:"roles"`
}

type MockUser struct {
	ID         string               `json:"id"`
	Username   string               `json:"username"`
	GlobalName string               `json:"global_name"`
	Avatar     string               `json:"avatar"`
	Guilds     map[string]MockGuild `json:"guilds"`
}

type MockConfig struct {
	// Tokens maps an authorization code to the user it authenticates.
	Tokens map[string]string `json:"tokens"`
	Users  map[string]MockUser `json:"users"`
}

func loadConfig(path string) (*MockConfig, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg MockConfig
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func main() {
	addr := flag.String("addr", "127.0.0.1:4010", "listen address")
	configPath := flag.String("config", "mock.json", "path to the scenario config")
	flag.Parse()

	mux := http.NewServeMux()

	// POST /oauth2/token — exchange the code for an access token.
	mux.HandleFunc("/oauth2/token", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if err := r.ParseForm(); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid_request"})
			return
		}
		cfg, err := loadConfig(*configPath)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"error": "config", "error_description": err.Error()})
			return
		}
		code := r.FormValue("code")
		if code == "" {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid_grant", "error_description": "Missing code"})
			return
		}
		userKey, ok := cfg.Tokens[code]
		if !ok {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid_grant", "error_description": "Invalid \"code\" in request."})
			return
		}
		if r.FormValue("redirect_uri") == "" || r.FormValue("client_secret") == "" {
			writeJSON(w, http.StatusBadRequest, map[string]any{"error": "invalid_request", "error_description": "Invalid \"redirect_uri\" in request."})
			return
		}
		log.Printf("[mock] token issued for code=%s user=%s redirect=%s", code, userKey, r.FormValue("redirect_uri"))
		writeJSON(w, http.StatusOK, map[string]any{
			"access_token": "tok-" + userKey,
			"token_type":   "Bearer",
			"expires_in":   604800,
			"refresh_token": "refresh-" + userKey,
			"scope":        "identify guilds guilds.members.read",
		})
	})

	// GET /users/@me — profile of the token owner.
	mux.HandleFunc("/users/@me", func(w http.ResponseWriter, r *http.Request) {
		cfg, err := loadConfig(*configPath)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"message": err.Error()})
			return
		}
		userKey, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer tok-")
		if !ok {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"message": "401: Unauthorized"})
			return
		}
		user, ok := cfg.Users[userKey]
		if !ok {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"message": "401: Unauthorized"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"id":            user.ID,
			"username":      user.Username,
			"global_name":   user.GlobalName,
			"avatar":        user.Avatar,
			"discriminator": "0",
		})
	})

	// GET /users/@me/guilds/{guild_id}/member — the gate's membership probe.
	mux.HandleFunc("/users/@me/guilds/", func(w http.ResponseWriter, r *http.Request) {
		cfg, err := loadConfig(*configPath)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"message": err.Error()})
			return
		}
		userKey, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer tok-")
		if !ok {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"message": "401: Unauthorized"})
			return
		}
		user, ok := cfg.Users[userKey]
		if !ok {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"message": "401: Unauthorized"})
			return
		}
		// /users/@me/guilds/{guild_id}/member
		rest := strings.TrimPrefix(r.URL.Path, "/users/@me/guilds/")
		parts := strings.Split(strings.Trim(rest, "/"), "/")
		if len(parts) != 2 || parts[1] != "member" {
			writeJSON(w, http.StatusNotFound, map[string]any{"message": "404: Not Found"})
			return
		}
		guildID := parts[0]
		guild, isMember := user.Guilds[guildID]
		if !isMember {
			// Discord answers 404 for a user that is not in the guild.
			writeJSON(w, http.StatusNotFound, map[string]any{"message": "404: Not Found", "code": 10004})
			return
		}
		roles := guild.Roles
		if roles == nil {
			roles = []string{}
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"user":   map[string]any{"id": user.ID, "username": user.Username},
			"roles":  roles,
			"guild_id": guildID,
		})
	})

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "ok")
	})

	log.Printf("[mock] Discord mock listening on %s (config %s)", *addr, *configPath)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatal(err)
	}
}
