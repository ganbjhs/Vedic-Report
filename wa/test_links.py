from wa.links import classify, clean_url, extract_urls
cases = {
 "https://www.instagram.com/p/C1abc23/?igsh=xyz": ("instagram","post"),
 "https://www.instagram.com/reel/C9zz/": ("instagram","post"),
 "https://www.instagram.com/p/C1abc23/c/17912345678901234/": ("instagram","comment"),
 "https://x.com/elonmusk/status/1234567890": ("twitter","post"),
 "https://twitter.com/user/status/99?s=20&t=abc": ("twitter","post"),
 "https://www.facebook.com/user/posts/pfbid0abc": ("facebook","post"),
 "https://www.facebook.com/user/posts/pfbid0abc?comment_id=123": ("facebook","comment"),
 "https://www.facebook.com/share/p/1abc/": ("facebook","post"),
 "https://www.facebook.com/groups/123/permalink/456/?reply_comment_id=9": ("facebook","comment"),
 "https://www.linkedin.com/posts/tilak_ai-activity-7123-abc": ("linkedin","post"),
 "https://www.linkedin.com/feed/update/urn:li:activity:7123?commentUrn=urn%3Ali%3Acomment%3A(activity%3A7123%2C999)": ("linkedin","comment"),
 "https://www.youtube.com/watch?v=abc123&lc=Ugxyz": ("youtube","comment"),
 "https://youtu.be/abc123?si=x": ("youtube","post"),
 "https://www.threads.net/@user/post/C9abc": ("threads","post"),
 "https://www.tiktok.com/@u/video/7123": ("tiktok","post"),
 "https://example.com/whatever": ("other","other"),
}
bad=0
for u,(pl,k) in cases.items():
    c=classify(u)
    ok = (c["platform"],c["kind"])==(pl,k)
    bad += not ok
    print("OK " if ok else "FAIL", c["platform"], c["kind"], "->", c["clean_url"])
print(extract_urls("see https://x.com/a/status/1 and https://instagram.com/p/x/."))
assert clean_url("https://twitter.com/user/status/99?s=20&t=abc")=="https://twitter.com/user/status/99"
assert clean_url("https://www.facebook.com/user/posts/pfbid0abc?comment_id=123&fbclid=zzz")=="https://facebook.com/user/posts/pfbid0abc?comment_id=123"
print("failures:",bad)
