# AWS isolation

- Do not call AWS APIs.
- Do not read cloud instance metadata for credentials.
- Only talk to GitHub via robomp host tools and to the colocated LiteLLM gateway.
