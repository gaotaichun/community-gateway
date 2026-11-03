// Command mockopenai is a minimal OpenAI-compatible upstream used by the
// end-to-end tests. It is NOT part of the shipped gateway.
//
//	go run ./tools/mockopenai -addr 127.0.0.1:4020
//
// Behaviour:
//   - POST /v1/chat/completions returns a fixed completion.
//     The "prompt" length controls the reported usage so tests can dial the
//     consumed quota: usage.prompt_tokens = 1000 by default.
//   - Authorization: Bearer <anything> is accepted; "bad-key" returns 401.
//   - model "missing/model" returns 404 model_not_found.
package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"strings"
	"time"
)

type chatRequest struct {
	Model    string `json:"model"`
	Messages []struct {
		Role    string `json:"role"`
		Content any    `json:"content"`
	} `json:"messages"`
}

func main() {
	addr := flag.String("addr", "127.0.0.1:4020", "listen address")
	promptTokens := flag.Int("prompt-tokens", 1000, "prompt tokens reported per request")
	completionTokens := flag.Int("completion-tokens", 200, "completion tokens reported per request")
	flag.Parse()

	mux := http.NewServeMux()

	mux.HandleFunc("/v1/models", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{
			"object": "list",
			"data": []any{
				map[string]any{"id": "minimax/abab6.5s-chat", "object": "model", "owned_by": "minimax"},
				map[string]any{"id": "openrouter/deepseek-chat", "object": "model", "owned_by": "openrouter"},
			},
		})
	})

	mux.HandleFunc("/v1/chat/completions", func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		if strings.Contains(auth, "bad-key") {
			writeJSON(w, 401, map[string]any{
				"error": map[string]any{"message": "invalid api key", "type": "invalid_request_error", "code": "invalid_api_key"},
			})
			return
		}
		var req chatRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		if req.Model == "missing/model" {
			writeJSON(w, 404, map[string]any{
				"error": map[string]any{"message": "model not found: missing/model", "type": "invalid_request_error", "code": "model_not_found"},
			})
			return
		}
		log.Printf("[mock-openai] model=%s prompt=%d completion=%d", req.Model, *promptTokens, *completionTokens)
		writeJSON(w, 200, map[string]any{
			"id":      "chatcmpl-mock",
			"object":  "chat.completion",
			"created": time.Now().Unix(),
			"model":   req.Model,
			"choices": []any{
				map[string]any{
					"index":         0,
					"message":       map[string]any{"role": "assistant", "content": "pong"},
					"finish_reason": "stop",
				},
			},
			"usage": map[string]any{
				"prompt_tokens":     *promptTokens,
				"completion_tokens": *completionTokens,
				"total_tokens":      *promptTokens + *completionTokens,
			},
		})
	})

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})

	log.Printf("[mock-openai] listening on %s", *addr)
	if err := http.ListenAndServe(*addr, mux); err != nil {
		log.Fatal(err)
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
