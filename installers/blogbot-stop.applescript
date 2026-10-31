-- Blogbot 중지기. osacompile로 .app으로 만들어 사용한다.
on run
	try
		do shell script "pkill -f 'blogbot web' || true"
		display notification "blogbot 서버를 종료했습니다." with title "Blogbot"
	on error errMsg
		display dialog "종료 실패:" & return & errMsg buttons {"확인"} default button 1
	end try
end run
