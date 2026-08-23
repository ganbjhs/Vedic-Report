#!/bin/sh
# Rebuild the UI bundle (only needed if you edit ui-src/). Requires: npm install (once).
cd "$(dirname "$0")"
npx esbuild app.jsx --bundle --minify --outfile=../ui/bundle.js --loader:.css=css --define:process.env.NODE_ENV=\"production\" --jsx=automatic
