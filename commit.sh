#!/bin/sh
set -e

# Стадируем все изменения (включая удаления)
git add -A

# Запросим сообщение коммита
printf "Commit message: "
IFS= read -r msg

# Если нечего коммитить — выходим
if git diff --cached --quiet; then
  echo "Nothing to commit."
  exit 0
fi

# Коммит и пуш в текущую ветку на указанный remote (по умолчанию origin)
git commit -m "$msg"
branch="$(git rev-parse --abbrev-ref HEAD)"
remote="${1:-origin}"
git push -u "$remote" "$branch" 