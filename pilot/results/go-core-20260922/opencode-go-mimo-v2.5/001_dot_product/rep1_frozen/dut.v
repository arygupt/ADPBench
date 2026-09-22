module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam NUM_BEATS = LEN / LANES; // 256/32 = 8
    localparam BEAT_CLOG = $clog2(NUM_BEATS); // 3
    localparam LANES_CLOG = $clog2(LANES+1); // 6 for count
    
    // State machine
    localparam S_IDLE = 2'd0;
    localparam S_ACCUM = 2'd1;
    localparam S_OUTPUT = 2'd2;
    
    reg [1:0] state, state_next;
    reg [BEAT_CLOG-1:0] beat_cnt, beat_cnt_next;
    
    // Accumulator
    reg signed [ACC_W-1:0] acc, acc_next;
    
    // Connect ready signals
    wire a_fire = in_a_flat_valid && in_a_flat_ready;
    wire b_fire = in_b_flat_valid && in_b_flat_ready;
    
    // Always ready in ACCUM state when not outputting, or accept input beats
    assign in_a_flat_ready = (state == S_ACCUM);
    assign in_b_flat_ready = (state == S_ACCUM);
    
    // Output assignment
    assign out_c = acc;
    assign out_valid = (state == S_OUTPUT);
    
    // Lane-level multiply-accumulate
    wire signed [ACC_W-1:0] lane_sum [0:LANES-1];
    wire signed [ACC_W-1:0] new_acc;
    
    genvar i;
    generate
        for (i = 0; i < LANES; i = i + 1) begin : gen_lanes
            wire signed [DATA_W-1:0] a_elem = in_a_flat[i*DATA_W +: DATA_W];
            wire signed [DATA_W-1:0] b_elem = in_b_flat[i*DATA_W +: DATA_W];
            wire signed [ACC_W-1:0] product = {{(ACC_W-DATA_W){a_elem[DATA_W-1]}}, a_elem} * {{(ACC_W-DATA_W){b_elem[DATA_W-1]}}, b_elem};
            assign lane_sum[i] = product;
        end
    endgenerate
    
    // Sum the lane products with a simple tree
    // 32 lanes, each 32-bit signed -> need careful sum
    wire signed [ACC_W-1:0] sum_all;
    
    // Simple 32-to-1 adder
    reg signed [ACC_W-1:0] partial0, partial1, partial2, partial3;
    reg signed [ACC_W-1:0] partial4, partial5, partial6, partial7;
    reg signed [ACC_W-1:0] sum01, sum23, sum45, sum67;
    reg signed [ACC_W-1:0] sum0123, sum4567;
    
    always_comb begin
        // Level 1: 4 groups of 8
        partial0 = lane_sum[0] + lane_sum[1] + lane_sum[2] + lane_sum[3] + lane_sum[4] + lane_sum[5] + lane_sum[6] + lane_sum[7];
        partial1 = lane_sum[8] + lane_sum[9] + lane_sum[10] + lane_sum[11] + lane_sum[12] + lane_sum[13] + lane_sum[14] + lane_sum[15];
        partial2 = lane_sum[16] + lane_sum[17] + lane_sum[18] + lane_sum[19] + lane_sum[20] + lane_sum[21] + lane_sum[22] + lane_sum[23];
        partial3 = lane_sum[24] + lane_sum[25] + lane_sum[26] + lane_sum[27] + lane_sum[28] + lane_sum[29] + lane_sum[30] + lane_sum[31];
        
        sum01 = partial0 + partial1;
        sum23 = partial2 + partial3;
        
        sum_all = sum01 + sum23;
    end
    
    assign new_acc = acc + sum_all;
    
    // State register
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
            beat_cnt <= '0;
            acc <= '0;
        end else begin
            state <= state_next;
            beat_cnt <= beat_cnt_next;
            acc <= acc_next;
        end
    end
    
    // Next state logic
    always_comb begin
        state_next = state;
        beat_cnt_next = beat_cnt;
        acc_next = acc;
        
        case (state)
            S_IDLE: begin
                // Start accepting inputs
                if (in_a_flat_valid && in_b_flat_valid) begin
                    state_next = S_ACCUM;
                    beat_cnt_next = '0;
                    acc_next = '0;
                end
            end
            
            S_ACCUM: begin
                if (a_fire && b_fire) begin
                    acc_next = new_acc;
                    if (beat_cnt == NUM_BEATS - 1) begin
                        state_next = S_OUTPUT;
                    end else begin
                        beat_cnt_next = beat_cnt + 1;
                    end
                end
            end
            
            S_OUTPUT: begin
                if (out_ready) begin
                    state_next = S_IDLE;
                end
            end
        endcase
    end

endmodule
