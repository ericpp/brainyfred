all:
	docker build --pull --no-cache -t ericpp/brainyfred --load .
upload:
	docker push ericpp/brainyfred
