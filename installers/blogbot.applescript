-- Blogbot 실행기. osacompile로 .app으로 만들어 사용한다.
-- 1) 서버가 이미 살아있는지 확인
-- 2) 안 살아있으면 백그라운드로 실행
-- 3) 서버가 응답할 때까지 잠깐 대기
-- 4) 기본 브라우저에서 대시보드를 연다

on run
	set projectDir to "/Users/blue/Blog"
	set serverURL to "http://127.0.0.1:8765"
	set healthCheck to "curl -sf " & serverURL & "/api/config > /dev/null"

	-- 이미 서버가 살아있는지
	try
		do shell script healthCheck
		-- 살아있으면 브라우저만 열고 종료
		open location serverURL
		return
	end try

	-- 서버 기동 (백그라운드)
	try
		do shell script "cd " & quoted form of projectDir & " && nohup ./blogbot.command > /tmp/blogbot-app.log 2>&1 &"
	on error errMsg
		display dialog "blogbot 서버를 실행할 수 없습니다:" & return & errMsg buttons {"확인"} default button 1 with icon stop
		return
	end try

	-- 최대 15초 대기하며 준비됐는지 폴링
	set ready to false
	repeat with i from 1 to 30
		delay 0.5
		try
			do shell script healthCheck
			set ready to true
			exit repeat
		end try
	end repeat

	if ready then
		open location serverURL
	else
		display dialog "서버가 응답하지 않습니다. 로그를 확인하세요:" & return & "/tmp/blogbot-app.log" buttons {"확인"} default button 1 with icon caution
	end if
end run
