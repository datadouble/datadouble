# datadouble-http

The read-only HTTP(S) storage adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/): `fetch` reads promoted
golden images from a web server, such as Apache or nginx behind basic auth or
a token, an artifact server or an internal file server. Use it when
non-production can reach that server over HTTPS but can neither mount its disk
nor call an S3 API.

```yaml
fetch:
  from:
    http:
      url: https://files.example.com/datadouble/
      auth:                                     # optional
        basic:
          username:
            env: EXPORT_USER
          password:
            env: EXPORT_PASSWORD
        # or bearer:
        #   env: EXPORT_TOKEN
      ca_bundle: /etc/ssl/internal-ca.pem       # optional
      timeout_seconds: 60                       # optional, per request
```

| Setting | Meaning |
|---|---|
| `url` | The URL the export store is served under. `http://` or `https://`, with no credentials, query or fragment. Store keys are paths under it. |
| `auth` | Optional. `basic` (a `username` and `password`) or `bearer` (a token sent as `Authorization: Bearer ...`). Each is a secret reference (`{env: NAME}` or `{file: path}`), never a value in the spec. `auth` needs an `https://` URL: credentials are never sent in clear text. |
| `ca_bundle` | Optional. A PEM file of the certificate authorities to trust instead of the default ones, for a server with an internal certificate. The default trust is httpx's: the certifi bundle, or the files `SSL_CERT_FILE` and `SSL_CERT_DIR` name, not the operating system's store. |
| `timeout_seconds` | Optional, default 60. How long to wait to connect, and between bytes of a response. |

## Read-only

The store only reads. Writing or deleting fails with an error naming the
store, so `http` belongs in `fetch.from`, never in `store` or `promote.to`.
`promote` writes with the filesystem or S3 store, in production, into the
directory the web server serves:

```yaml
promote:
  to:
    - filesystem:                                   # served as https://files.example.com/datadouble/
        path: /srv/exports/datadouble
fetch:
  from:
    http:
      url: https://files.example.com/datadouble/
      auth:
        bearer:
          env: EXPORT_TOKEN
```

The web server must be able to read the files `promote` writes. The
filesystem store creates each file readable only by the user `promote` runs
as, so run the server as that user, or have it read the directory some other
way your platform allows. A file the server cannot read usually fails
`fetch` with `HTTP 403`.

`prune --target export` runs against the store `promote` writes to, not
this one.

## Listing: the index

Web servers have no standard way to list files, and `fetch` lists the store
to find each group's image lines (engine compatibility lines). So `promote` writes an
index, `<spec>/<group>/index.json`, beside the images on every export store,
naming the group's image lines and the generations with a summary in each,
and rewrites it after each image's summary and channel; `prune --target
export` rewrites it too, and removes it when nothing is left. Over `http`,
`fetch` reads only the index's line names; it then reads the line's channel
and the summary it points at directly. The `http` store needs nothing
server-specific: no directory listing has to be turned on. The store's other
files and layout are unchanged.

To rebuild the index, `promote` lists each export store, so an S3 export
store needs `s3:ListBucket` on its bucket for the credentials `promote` uses.

An export store with no index, such as one filled by copying files rather
than by `promote`, shows no images over `http`; promoting to it writes one.

## Errors and credentials

A 404 means the key is not there. Any other error status fails, naming the
URL path and the HTTP status; connection failures name the kind of failure,
and a certificate the CA bundle does not trust says "certificate
verification failed". Neither names response bodies or credentials. Credentials are resolved from
their secret references when the store is opened, and passed to the CI
system's log masking. `fetch` checks signatures and hashes exactly as with
any other store: the Sigstore bundle is just another key.

Redirects are followed; httpx drops the `Authorization` header when one leads
to another host. Archives are downloaded with `Accept-Encoding: identity` and
written as the server sends them, so a server's compression cannot change
the bytes `fetch` checks against the summary's hash.
