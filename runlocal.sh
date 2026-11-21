#!/bin/sh
# usage: runlocal.sh IMAGE DATA_DIR OUT
docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume $2/commits:/data/commits:ro --volume $2/tests.json:/data/tests.json:ro \
  --entrypoint python $1 -c 'import resource,subprocess,sys,time; t=time.time(); subprocess.run(["python","/app/br/selector.py"]); r=resource.getrusage(resource.RUSAGE_CHILDREN); print("CONTAINER_CPU %.2f wall %.2f" % (r.ru_utime+r.ru_stime, time.time()-t), file=sys.stderr)' > $3
