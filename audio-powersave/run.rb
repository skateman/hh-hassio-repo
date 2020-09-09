#!/usr/bin/env ruby
# frozen_string_literal: true

require 'concurrent'
require 'json'
require 'net/http'
require 'set'
require 'uri'
require 'logger'

@config = JSON.parse(File.read('/data/options.json'))

HEADERS = {
  'Accept-Encoding' => 'none',
  'Authorization' => "Bearer #{ENV['HASSIO_TOKEN']}",
  'Content-Type' => 'application/json'
}.freeze

@sinks = Set.new
@logger = Logger.new(STDOUT, level: @config['log_level'].to_sym)

def path(onoff)
  case onoff
  when 'on'
    @config['on_path']
  when 'off'
    @config['off_path']
  end
end

def switch(onoff)
  return if @state == onoff

  @logger.info("Turning audio #{onoff}")

  response = Net::HTTP.post(URI("http://hassio/homeassistant/api/#{path(onoff)}"), @config['payload'].to_json, HEADERS)

  @logger.debug("Turned audio #{onoff} with #{response.inspect}")

  @state = onoff
rescue => e
  @logger.error(e.inspect)
end

def turn_on
  @schedule&.cancel
  switch('on')
end

def cleanup
  return if @sinks.any? || @schedule&.pending?

  @schedule = Concurrent::ScheduledTask.execute(@config['timeout']) { switch('off') }
end

@logger.info('Starting the subscription...')

IO.popen('pactl subscribe') do |io|
  loop do
    text = io.readline
    @logger.debug(text)
    case text
    when /^Event 'new' on sink-input \#(\d+)/
      @sinks.add(Regexp.last_match[1])
      turn_on
    when /^Event 'remove' on sink-input \#(\d+)/
      @sinks.delete(Regexp.last_match[1])
      cleanup
    end
  end

rescue Interrupt
  @logger.info('Exiting...')
rescue => e
  @logger.error(e.inspect)
end
