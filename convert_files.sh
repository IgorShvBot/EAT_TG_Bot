mkdir -p txt_versions

for f in docker-compose.yml Dockerfile bot.py extract_transactions_pdf1.py extract_transactions_pdf2.py classify_transactions_pdf.py database.py; do
    if [[ "$f" == "Dockerfile" ]]; then
        cp "$f" "txt_versions/Dockerfile.txt"
    else
        newname="${f%.*}.txt"
        cp "$f" "txt_versions/$newname"
    fi
done

# Копируем requirements.txt как есть
if [[ -f "requirements.txt" ]]; then
    cp "requirements.txt" "txt_versions/"
else
    echo "Файл requirements.txt не найден!"
fi

# bash convert_files.sh