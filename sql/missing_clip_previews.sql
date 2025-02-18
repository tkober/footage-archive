SELECT F.md5_hash,
       F.file_name,
       F.directory || '/' || F.file_name AS file_path
FROM Files F
         LEFT JOIN ClipPreviews CP ON F.md5_hash = CP.md5_hash
WHERE CP.md5_hash IS NULL
