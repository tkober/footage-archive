SELECT F.md5_hash,
       F.file_name,
       CONCAT(F.directory, '/', F.file_name) as file_path
FROM Files F
         LEFT JOIN ClipPreviews CP ON F.md5_hash = CP.md5_hash
WHERE CP.md5_hash IS NULL
